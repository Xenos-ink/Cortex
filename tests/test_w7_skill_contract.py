"""The ``cortex-skill`` operating-manual contract, ported to the stable 0.7.6 tree (WP9).

Ported from the dev copy's ``test_w7_skill_contract.py`` and ADAPTED to the stable tree:

* the served surface is the TEN-tool set (``computer_wait_status`` joined at WP6; the
  dev copy asserted nine);
* imports follow the layered restructure (``core`` / ``desktop`` / ``pipeline`` /
  ``server`` / ``vision`` packages) — no ``computer_use_mcp.server`` module-level
  attribute surface exists anymore;
* the four "execution patterns" are re-pinned as the four execution surfaces the
  rewritten core teaches (single action, ``follow_ups`` batch, waypoint ``plan``,
  wait family), and the plan detail moved to ``references/plan-protocol.md``;
* the dev README assertions are DROPPED here: the README/VERSIONS/docs update is
  WP10's scope and the stable README still describes v0.7.5 (recorded in
  wp9-notes.md as WP10 work, not silently ignored);
* ``computer_wait`` is now the BLOCKING segment wait (Addendum 2 / WP6), so the dev
  "sync tool, fire-and-forget handle" pins are replaced by the blocking-segment
  contract;
* the ``uncertain`` doctrine pins are re-targeted at the rewritten core + the
  ``references/examples.md`` worked examples.

Two properties make this a contract and not a snapshot (unchanged from dev):

1. Every cap is read from the product, not typed in — change a cap in code without
   restating it here and this file fails.
2. No false claims — the negative pins guard the historical falsehoods the repo has
   shipped (``delta 0..20``, a never-launch default that launches, a hidden 50 ms
   settle).

Prose is asserted against a WHITESPACE-FLATTENED view of the skill (core + all four
references), because the manual is hard-wrapped; a genuine reword still fails.
"""

from __future__ import annotations

import asyncio
import inspect
import re
from pathlib import Path

import pytest

from computer_use_mcp.cli import EXPECTED_TOOLS
from computer_use_mcp.core.limits import Limits
from computer_use_mcp.core.models import (
    MAX_DURATION_MS,
    MAX_FOLLOW_UPS,
    MAX_HOLD_MS,
    MAX_PLAN_SETTLE_MS,
)
from computer_use_mcp.desktop.backend import MOVE_DURATION_MS, MOVE_SETTLE_MS
from computer_use_mcp.desktop.motion import WAIT_MAX_SECONDS
from computer_use_mcp.planning.plan import (
    MAX_PLAN_ACTIONS_PER_POINT,
    MAX_PLAN_POINTS,
    MAX_PLAN_TIMEOUT_MS,
)
from computer_use_mcp.server.app import mcp
from computer_use_mcp.server.wait_machine import (
    MAX_WAITS_GLOBAL,
    MAX_WAITS_PER_SESSION,
    WAIT_TOMBSTONES_PER_SESSION,
)
from computer_use_mcp.vision.evidence import (
    EVIDENCE_MAX_AGE_SECONDS,
    EVIDENCE_MAX_BYTES,
    EVIDENCE_MAX_FRAMES,
    EVIDENCE_REFUSE_TEXT_SESSION,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = REPO_ROOT / "skills" / "cortex-skill"
REFERENCE_NAMES = (
    "plan-protocol.md",
    "reply-contract.md",
    "glossary.md",
    "examples.md",
)
CORE_MAX_LINES = 250


def _core() -> str:
    return (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")


def _reference(name: str) -> str:
    return (SKILL_DIR / "references" / name).read_text(encoding="utf-8")


def _flat() -> str:
    return re.sub(r"\s+", " ", "\n".join([_core()] + [_reference(n) for n in REFERENCE_NAMES]))


def _registered_tool_names() -> set[str]:
    return {tool.name for tool in asyncio.run(mcp.list_tools())}


# --- the served surface is the documented surface ------------------------------------------------


def test_the_served_surface_is_exactly_the_ten_documented_tools() -> None:
    """The manual's tool table must BE the served surface, never a hand-kept copy."""
    assert _registered_tool_names() == set(EXPECTED_TOOLS), (
        "the served tool surface changed; the skill's tool table is a contract, so it "
        "must be restated in the SAME change that adds or removes a tool"
    )
    assert len(EXPECTED_TOOLS) == 10


def test_the_skill_documents_every_registered_tool_in_its_own_table_row() -> None:
    skill = _core()
    for name in sorted(_registered_tool_names()):
        assert f"| `{name}` |" in skill, f"{name!r} has no row in the skill's tool table"


def test_the_core_stays_within_the_line_budget_and_links_the_references() -> None:
    """The rewrite's headline economy gate: the core is a core, not the whole manual."""
    lines = _core().splitlines()
    assert len(lines) <= CORE_MAX_LINES, (
        f"the core grew to {len(lines)} lines (budget {CORE_MAX_LINES}); move detail "
        "into references/ instead"
    )
    for name in REFERENCE_NAMES:
        assert (SKILL_DIR / "references" / name).exists()
        assert f"references/{name}" in _core()


def test_the_core_names_the_four_execution_surfaces() -> None:
    """Adapted from the dev four-patterns pin: single action, batch, plan, wait family."""
    flat = _flat()
    assert "`follow_ups`" in flat
    assert "`plan`" in flat
    assert "## The wait family" in _core()
    assert "references/plan-protocol.md" in _core()


def test_the_skill_states_the_observation_grounding_execution_verification_loop() -> None:
    flat = _flat()
    for stage in ("observation", "grounding", "execution", "verification"):
        assert stage in flat, f"the skill never names the {stage!r} stage"
    assert "Execution is not effect" in flat


# --- the ``uncertain`` doctrine (ported, re-targeted at the rewritten core) -----------------------


def test_the_uncertain_doctrine_says_it_is_neither_success_nor_failure() -> None:
    flat = _flat()
    assert "`uncertain` = dispatched but unproven — never success, never failure" in flat


def test_the_uncertain_doctrine_is_true_of_the_verification_engine() -> None:
    """A red-team tie-back: the skill must not promise more than the code does."""
    from computer_use_mcp.pipeline import verification

    source = re.sub(r"\s+", " ", inspect.getsource(verification))
    assert "``uncertain`` is NEVER success" in source, (
        "verification.py no longer states the doctrine the skill teaches; restate both"
    )
    assert "No code path in this module maps ``uncertain`` to ``verified``." in source
    result = verification._uncertain("test", "note", [], 0.0, False)
    assert result.outcome == "uncertain"
    assert "never success" in _flat()


def test_the_uncertain_doctrine_forbids_deciding_from_confidence_alone() -> None:
    flat = _flat()
    assert "`confidence` is not a success probability" in flat
    assert "a number is not visible proof" in flat


def test_the_uncertain_doctrine_forbids_a_blind_re_issue() -> None:
    flat = _flat()
    assert "NEVER re-issue an action whose first attempt may have landed" in flat
    assert "double-apply" in flat


def test_the_uncertain_doctrine_requires_reading_the_attached_or_fetchable_image() -> None:
    """F3 rewrite: a reply without an image retained nothing — the driver re-observes
    instead of hunting for a fetchable id."""
    flat = _flat()
    assert "If the reply carried no image, nothing was retained for that call" in flat
    assert "do not hunt for a fetchable id" in flat
    assert "computer_observe" in flat


def test_the_uncertain_doctrine_requires_preserving_insufficient_evidence() -> None:
    flat = _flat()
    assert "report the step as unproven" in flat.casefold()
    assert "do not claim either way" in flat


def test_the_uncertain_doctrine_carries_the_worked_save_example() -> None:
    """The user's mandated example: a save click that returns ``uncertain``."""
    examples = _reference("examples.md")
    assert "## The save click" in examples
    assert "You click **Save** in an editor" in examples
    assert "Do not click Save again just because the verdict was not `verified`" in examples


def test_the_uncertain_doctrine_has_both_kinds_of_example() -> None:
    examples = _reference("examples.md")
    assert "### (a) When the image settles the question" in examples
    assert "### (b) When the image cannot settle it" in examples
    after_b = examples.split("### (b) When the image cannot settle it", 1)[1]
    assert "Keep the uncertainty" in after_b
    assert "computer_observe" in after_b
    assert "computer_fetch_image" in after_b
    assert "stop guessing" in after_b


def test_the_uncertain_doctrine_does_not_contradict_the_marker_rule() -> None:
    flat = _flat()
    assert "never about the task being finished" in flat
    assert "`verification_method: no_op_marker`" in flat


# --- §7.5 · the caps the skill states are the caps the code enforces ------------------------------


def test_the_skill_states_the_enforced_flat_wait_cap() -> None:
    assert WAIT_MAX_SECONDS == 10.0
    assert "capped at 10 seconds" in _flat()


def test_the_skill_keeps_the_10s_flat_cap_and_the_3h_tool_cap_distinct() -> None:
    """The two waits are DIFFERENT products; conflating them is the defect to prevent."""
    from computer_use_mcp.server.wait_machine import WAIT_HARD_MAX_SECONDS

    assert WAIT_HARD_MAX_SECONDS == 10800.0
    assert Limits().max_wait_seconds == 10800.0
    flat = _flat()
    assert "10 800 s (3 h)" in flat
    assert "capped at 10 seconds" in flat
    assert "different product" in flat


def test_the_skill_states_the_enforced_plan_caps() -> None:
    assert MAX_PLAN_POINTS == 10
    assert MAX_PLAN_ACTIONS_PER_POINT == 5
    assert MAX_HOLD_MS == 5_000
    assert MAX_DURATION_MS == 5_000
    assert MAX_PLAN_SETTLE_MS == 200
    assert MAX_PLAN_TIMEOUT_MS == 60_000
    flat = _flat()
    for value, what in (
        ("at most 10 points", "MAX_PLAN_POINTS"),
        ("at most 5 actions", "MAX_PLAN_ACTIONS_PER_POINT"),
        ("0..5000 ms", "MAX_HOLD_MS / MAX_DURATION_MS"),
        ("0..200 ms", "MAX_PLAN_SETTLE_MS"),
        ("0..60000 ms", "MAX_PLAN_TIMEOUT_MS"),
    ):
        assert value in flat, f"the skill never states {what} ({value})"


def test_the_skill_states_the_enforced_wait_concurrency_caps() -> None:
    assert MAX_WAITS_PER_SESSION == 4
    assert MAX_WAITS_GLOBAL == 16
    assert WAIT_TOMBSTONES_PER_SESSION == 8
    flat = _flat()
    assert "4 per session and 16 process-wide" in flat
    assert "`too_many_waits`" in flat
    assert "only the last 8 finished waits are remembered" in flat


def test_the_skill_states_the_enforced_evidence_retention() -> None:
    assert EVIDENCE_MAX_FRAMES == 16
    assert EVIDENCE_MAX_BYTES == 24 * 1024 * 1024
    assert EVIDENCE_MAX_AGE_SECONDS == 3600.0
    assert "16 frames / 24 MB / 3600 s" in _flat()


def test_the_skill_states_the_instant_outside_plan_motion_defaults() -> None:
    """SPEC 5.1: outside a plan both the move duration and the settle are 0 ms.

    ENV-INDEPENDENT (VAL F12): the module constants are resolved from the environment
    at import, so the PIN reads the knob DEFAULTS from the backend source instead of
    the resolved values.
    """
    backend_src = (REPO_ROOT / "src" / "computer_use_mcp" / "desktop" / "backend.py").read_text(
        encoding="utf-8"
    )
    move_default = int(
        re.search(r"MOVE_DURATION_MS = _env_bounded_int\([^,]+, (\d+)", backend_src).group(1)
    )
    settle_default = float(
        re.search(r"MOVE_SETTLE_MS = _env_bounded_float\([^,]+, ([0-9.]+)", backend_src).group(1)
    )
    assert (move_default, settle_default) == (0, 0.0)
    assert "both motion and settle are instant (0 ms / 0 ms)" in _flat()


def test_the_skill_lists_the_explicit_mode_as_the_reason_plans_must_be_explicit() -> None:
    flat = _flat()
    assert "EXPLICIT-TIMING mode" in flat
    for field in (
        "transition_duration_ms",
        "duration_ms",
        "final_wait_ms",
        "settle_ms",
        "arrival_condition",
        "point_timeout_ms",
        "timeout_ms",
        "on_failure",
        "on_uncertain",
    ):
        assert f"`{field}`" in flat, f"the plan requirements omit {field!r}"


def test_the_skill_states_the_evidence_age_limit_honestly() -> None:
    flat = _flat()
    assert "A fetched image is a picture of `captured_at`, not the present" in flat
    assert "`age_seconds`" in flat
    assert "look BACK" in flat

def test_the_skill_states_that_a_text_session_has_no_retrievable_image() -> None:
    assert EVIDENCE_REFUSE_TEXT_SESSION == "text_session_no_image"
    flat = _flat()
    assert '`image_delivery="text"`' in flat
    assert "never produces image bytes at all" in flat


def test_the_skill_states_the_real_end_of_session_conditions() -> None:
    """``max_task_seconds`` is advertised and enforced NOWHERE.

    ``LimitEnforcer.check_task_duration`` has no production call site, so a skill that
    told the model a wall clock ends its work would be teaching a fiction. (Adapted
    from dev: the count now spans the layered package layout.)
    """
    src = REPO_ROOT / "src" / "computer_use_mcp"
    candidates = [src / "core" / "limits.py", src / "pipeline" / "agent.py", src / "vision" / "evidence.py"]
    candidates += sorted((src / "server").glob("*.py"))
    call_sites = sum(c.read_text(encoding="utf-8").count("check_task_duration(") for c in candidates)
    assert call_sites == 1, (
        "check_task_duration gained a production call site; if the wall-clock ceiling "
        "is now operative the skill's 'what actually ends a session' section must be restated"
    )
    flat = _flat()
    assert "What actually ends a session" in flat
    assert "not a clock that ends your work" in flat


# --- the wait family, cancel contract, and fetch are documented as products -----------------------


def test_the_skill_documents_the_blocking_segment_wait_loop() -> None:
    """Adapted from the dev fire-and-forget pins: WP6 made computer_wait BLOCKING."""
    from computer_use_mcp.server.wait import computer_wait

    assert inspect.iscoroutinefunction(computer_wait), (
        "computer_wait stopped being async; the blocking-segment premise changed and "
        "the skill's wait section must be restated"
    )
    flat = _flat()
    assert "BLOCKS up to one segment" in flat
    assert "default 45 s" in flat
    assert "status:\"completed\"" in flat
    assert "status:\"running\"" in flat
    assert "`computer_wait_status`" in flat
    assert "Splitting a long wait into several waits is visible, not a workaround" in flat


def test_the_skill_documents_the_cancel_contract_including_its_ambiguities() -> None:
    flat = _flat()
    assert "computer_cancel_wait" in flat
    assert "`already_cancelled:true`" in flat
    assert "byte-identical" in flat
    assert "`unknown_wait`" in flat
    assert "`wait_failed`" in flat
    assert "zero `max_actions` slots" in flat


def test_the_skill_documents_fetch_image_as_a_look_back_not_a_look_forward() -> None:
    from computer_use_mcp.server.fetch import computer_fetch_image

    assert inspect.iscoroutinefunction(computer_fetch_image)
    flat = _flat()
    assert "`image_not_found`" in flat
    assert "`image_expired`" in flat
    assert "look BACK" in flat


def test_the_skill_documents_the_lease_refusals_and_the_two_read_only_exceptions() -> None:
    """A blocked plan session refuses eight surfaces; fetch and wait_status stay allowed."""
    flat = _flat()
    assert "`plan_lease`" in flat
    assert "`computer_fetch_image` stays allowed during a lease" in flat
    assert "`computer_wait_status` stays allowed during a lease" in flat
    for verb in ("continue", "stop", "replace"):
        assert f'"{verb}"' in _reference("plan-protocol.md"), verb


# --- no claim the code contradicts ----------------------------------------------------------------


def test_the_skill_makes_no_claim_the_code_contradicts() -> None:
    """Guard the historical falsehoods this repo has actually shipped."""
    flat = _flat()
    # ACTION_VOCABULARY once taught 0..20 against a 10 s cap.
    assert "delta 0..20" not in flat
    assert "0..20 seconds" not in flat
    # The never-launch default is false — the default is launch="server".
    assert "the server never launches by default" not in flat
    assert "never launches by default" not in flat
    assert 'launch="driver"' in flat
    # SPEC 5.1 removed the hidden 50 ms settle; a plan that WANTS one now says so.
    assert "settle is 50 ms" not in flat
    assert "default 50" not in flat
    # The scroll sign convention must stay with its cursor dependence.
    assert "POSITIVE = UP" in flat and "CURRENT CURSOR POSITION" in flat


def test_the_skill_names_the_exact_launch_restore_knob() -> None:
    """Replaces the dev's never-failing 'driverr' loop (VAL F12): the restore knob
    must be spelled exactly, and the policy parser must accept ONLY that spelling."""
    flat = _flat()
    assert "CORTEX_ATTACH_OR_LAUNCH=driver" in flat
    from computer_use_mcp.desktop import interference

    source = inspect.getsource(interference)
    assert 'casefold() == "driver"' in source, (
        "the parser stopped accepting only the exact spelling; re-check the skill text"
    )


def test_the_rejection_double_apply_claim_is_never_absolute() -> None:
    """K-05: the mid-type drift rejection lands AFTER part of the text was typed."""
    flat = _flat()
    assert "A rejection dispatched nothing" not in flat
    assert "cannot double-apply" not in flat
    assert "`partial_delivery`" in flat
    assert "`delivered_chars`" in flat


def test_the_frontmatter_names_the_session_entry_tools_and_natural_triggers() -> None:
    head = _core().split("---", 2)[1]
    for name in ("start_session", "computer_execute", "computer_observe"):
        assert name in head
    for trigger in ("click", "type", "Windows app", "desktop"):
        assert trigger in head


@pytest.mark.parametrize(
    "block",
    (
        # The wait family's long-wait rule.
        "a different product from the wait tools",
        # The four-state teaching was replaced by the retention truth (VAL F3).
        "A missing image is not a missing effect",
        # The flat wait's clamp annotation.
        "`waited_seconds=<effective>`",
        # The scroll sign facts.
        "POSITIVE = UP",
        "CURRENT CURSOR POSITION",
    ),
)
def test_the_teaching_blocks_are_still_present(block: str) -> None:
    assert block in _flat(), f"the rewrite dropped the teaching block {block!r}"
