"""WP9 — the shipped cortex-skill states what the stable server at HEAD enforces.

DELTA VAL-WP9-01: every expected skill string is DERIVED from the code constant it
mirrors (no ``== 5_000``-style literals standing in for code values); the
verification_method tokens are read from the emitted strategy names plus the agent's
special tiers; the retention, resubmit, policy-stop, failure-class, interference-class
and integrity teachings are pinned against the code lines that govern them; and the
negative pins cover the full FALSE/UNSUPPORTED sentence list from VAL-WP9-01.

Every assertion reads the FACT from the code first and only then requires the SHIPPED
skill text (``skills/cortex-skill/SKILL.md`` + ``references/*.md``, paths relative to
the repo root) to state that fact consistently. Whitespace-normalized matching
(``_flat``) because the manual is hard-wrapped; a genuine reword still fails.
"""

from __future__ import annotations

import asyncio
import inspect
import re
from pathlib import Path
from typing import Any, get_args

import pytest

from computer_use_mcp.cli import EXPECTED_TOOLS
from computer_use_mcp.core.limits import Limits
from computer_use_mcp.core.models import (
    MAX_DURATION_MS,
    MAX_FOLLOW_UPS,
    MAX_HOLD_MS,
    MAX_PLAN_SETTLE_MS,
    OK_TRUE_RESULTS,
    RESULT_TOKENS,
    ActionSpec,
    EnsureAppOutcome,
    GroundedAction,
    Point,
)
from computer_use_mcp.desktop.motion import (
    WAIT_CLAMPED_MARKER,
    WAIT_MAX_SECONDS,
)
from computer_use_mcp.planning.plan import (
    MAX_PLAN_ACTIONS_PER_POINT,
    MAX_PLAN_POINTS,
    MAX_PLAN_TIMEOUT_MS,
    PLAN_EVIDENCE_KEY,
    PLAN_FAILURE_REASONS,
    TERMINAL_PLAN_RESULTS,
)
from computer_use_mcp.server.app import mcp
from computer_use_mcp.server.lifecycle import (
    SESSION_IDLE_TIMEOUT_DEFAULT_SECONDS,
    SESSION_IDLE_TIMEOUT_ENV,
    SESSION_IDLE_TIMEOUT_MAX_SECONDS,
    SESSION_IDLE_TIMEOUT_MIN_SECONDS,
)
from computer_use_mcp.server.wait_machine import (
    MAX_WAITS_GLOBAL,
    MAX_WAITS_PER_SESSION,
    WAIT_HARD_MAX_SECONDS,
    WAIT_SEGMENT_DEFAULT_SECONDS,
    WAIT_SEGMENT_MAX_SECONDS,
    WAIT_SEGMENT_MIN_SECONDS,
    WAIT_TOMBSTONES_PER_SESSION,
)
from computer_use_mcp.vision.evidence import (
    EVIDENCE_MAX_AGE_SECONDS,
    EVIDENCE_MAX_BYTES,
    EVIDENCE_MAX_FRAMES,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = REPO_ROOT / "skills" / "cortex-skill"
SRC_DIR = REPO_ROOT / "src" / "computer_use_mcp"
REFERENCE_FILES = (
    "plan-protocol.md",
    "reply-contract.md",
    "glossary.md",
    "examples.md",
)
_NUM_WORDS = {10: "ten"}


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _core() -> str:
    return _read(SKILL_DIR / "SKILL.md")


def _reference(name: str) -> str:
    return _read(SKILL_DIR / "references" / name)


def _all_text() -> str:
    return "\n".join([_core()] + [_reference(name) for name in REFERENCE_FILES])


def _flat(text: str | None = None) -> str:
    return re.sub(r"\s+", " ", _all_text() if text is None else text)


def _field_bound(model: Any, name: str) -> tuple[int | None, int | None]:
    ge: int | None = None
    le: int | None = None
    for constraint in model.model_fields[name].metadata:
        ge = getattr(constraint, "ge", ge)
        le = getattr(constraint, "le", le)
    return ge, le


def _max_length(model: Any, name: str) -> int | None:
    for constraint in model.model_fields[name].metadata:
        value = getattr(constraint, "max_length", None)
        if value is not None:
            return int(value)
    return None


def _src(name: str) -> str:
    return _read(SRC_DIR / name)


# --- the served surface and the documented surface are the same set ------------------------------


def _registered_tool_names() -> set[str]:
    return {tool.name for tool in asyncio.run(mcp.list_tools())}


def test_the_served_surface_is_exactly_the_documented_ten_tools() -> None:
    assert _registered_tool_names() == set(EXPECTED_TOOLS), (
        "the served tool surface changed; the skill is a contract and must be "
        "restated in the SAME change that adds or removes a tool"
    )


def test_the_core_names_the_tool_count_from_the_live_surface() -> None:
    count = len(EXPECTED_TOOLS)
    assert _NUM_WORDS[count] == "ten"
    assert f"## The {_NUM_WORDS[count]} tools" in _core()
    assert f"the {_NUM_WORDS[count]} Cortex tools" in _core()


def test_the_skill_names_every_registered_tool_in_its_own_table_row() -> None:
    skill = _core()
    for name in sorted(_registered_tool_names()):
        assert f"| `{name}` |" in skill, f"{name!r} has no row in the skill's tool table"


def test_the_core_links_every_reference_file_by_relative_path() -> None:
    skill = _core()
    for name in REFERENCE_FILES:
        assert f"references/{name}" in skill, f"the core never links references/{name}"


# --- the reply contract (master-mission §1) as the code publishes it -----------------------------


def test_the_plan_evidence_key_is_evidence_by_backend() -> None:
    assert PLAN_EVIDENCE_KEY == "evidence_by_backend"
    flat = _flat()
    assert "`evidence_by_backend`" in flat
    assert "plan step and terminal replies carry `evidence_by_backend`" in flat
    # The historical falsehood (A3 K-46/K-57/K-81) AND its near neighbours: `evidence`
    # is never used as a reply KEY with a payload anywhere in the manual.
    assert "`evidence` (`backend.executed`" not in flat
    assert "evidence.repositions" not in flat
    assert re.search(r"`evidence`\s*\(", flat) is None


def test_the_approval_reply_key_is_requires_approval() -> None:
    flat = _flat()
    assert "`requires_approval:true`" in flat
    for line in _all_text().splitlines():
        if "approval_required" in line:
            assert "failure_reason" in line, f"approval_required used outside its value context: {line!r}"
    assert "ok:false, result:\"not_executed\", requires_approval:true" in flat


def test_the_closed_result_token_set_is_taught() -> None:
    assert RESULT_TOKENS == (
        "verified",
        "uncertain",
        "failed",
        "rejected",
        "not_executed",
        "dry_run",
        "marker",
        "stopped",
        "unknown",
    )
    flat = _flat()
    for token in RESULT_TOKENS:
        assert f"`{token}`" in flat, f"the skill never names the result token {token!r}"
    assert OK_TRUE_RESULTS == frozenset({"verified", "marker"})
    assert "ok is True iff result is `verified` or `marker`" in flat


def test_the_flat_result_subset_includes_not_executed() -> None:
    """F10a: the approval gate on the flat surface is result not_executed
    (server/execute.py approval branch), so the taught subset must carry it."""
    import computer_use_mcp.server.execute as execute_module

    execute_src = inspect.getsource(execute_module)
    assert '"result": "not_executed"' in execute_src
    assert '"requires_approval": True' in execute_src
    flat = _flat()
    assert (
        "the flat execute surface and follow-up items allow verified, uncertain, failed, rejected, not_executed, dry_run, marker and stopped" in flat
    )


def test_the_terminal_plan_ok_rule_is_stated() -> None:
    from computer_use_mcp.planning.plan import plan_terminal_outcome

    flat = _flat()
    for token in TERMINAL_PLAN_RESULTS:
        assert f"`{token}`" in flat, f"the terminal set is missing {token!r}"
    assert "ok is True only for `result: \"verified\"`" in flat
    assert "`unknown` and `stopped` are never success" in flat
    outcome = plan_terminal_outcome([{"result": "uncertain", "ok": False}], expected_items=1)
    assert outcome.result == "unknown" and outcome.ok is False


def test_the_plan_failure_reason_vocabulary_is_taught_closed() -> None:
    assert len(PLAN_FAILURE_REASONS) == 16
    assert "unclassified" in PLAN_FAILURE_REASONS
    flat = _flat()
    for token in sorted(PLAN_FAILURE_REASONS):
        assert f"`{token}`" in flat, f"the failure_reason vocabulary omits {token!r}"
    assert "closed vocabulary" in flat


def test_the_failure_class_mapping_is_conditional_on_the_first_item() -> None:
    """F6 (A3 K-27): the focus_drifted/rejected class mapping holds only when a LATER
    item of the step is refused; a first-item refusal maps through the kind table."""
    plan_src = _src("planning") if False else _read(SRC_DIR / "planning" / "plan.py")
    assert '"focus_taken_by": "focus_drifted"' in plan_src
    assert '"focus_identity_unknown": "focus_drifted"' in plan_src
    assert '"stuck_modifier": "rejected"' in plan_src
    assert '"target_gone": "rejected"' in plan_src
    assert '_PLAN_OUTCOME_KIND_TO_FAILURE_REASON' in plan_src
    assert '"rejected": "rejected"' in plan_src
    flat = _flat()
    assert "when a LATER item of a step is refused" in flat
    assert "when the step's FIRST item is refused" in flat
    assert "ALL of them report `rejected`" in flat


def test_the_ensure_app_outcome_enum_is_taught() -> None:
    outcomes = tuple(sorted(get_args(EnsureAppOutcome)))
    assert outcomes == (
        "ambiguous",
        "launch_rejected",
        "launch_unresolved",
        "launched",
        "not_found",
        "reattached",
    )
    flat = _flat()
    assert "`ensure_app_outcome`" in flat
    for value in outcomes:
        assert f"`{value}`" in flat, f"ensure_app_outcome value {value!r} is not taught"


def _emitted_verification_methods() -> set[str]:
    """The verification_method tokens the code actually emits: the strategies' own
    ``name`` properties plus every literal stamped in agent.py / verification.py."""
    from computer_use_mcp.pipeline import agent as agent_module
    from computer_use_mcp.pipeline import verification as verification_module
    from computer_use_mcp.pipeline.verification import (
        DeterministicPredicateStrategy,
        FocusChangeStrategy,
        ProcessStateStrategy,
        ScreenshotDiffStrategy,
        TextPredicateStrategy,
        UiControlTextStrategy,
        WindowStateStrategy,
    )

    emitted: set[str] = set()
    for strategy in (
        ScreenshotDiffStrategy(),
        WindowStateStrategy(),
        ProcessStateStrategy(),
        DeterministicPredicateStrategy(),
        TextPredicateStrategy(),
        UiControlTextStrategy(),
        FocusChangeStrategy(),
    ):
        emitted.add(strategy.name)
    for source in (
        inspect.getsource(agent_module),
        inspect.getsource(verification_module),
    ):
        emitted |= set(re.findall(r'verification_method="([a-z_]+)"', source))
    emitted.discard("none")
    return emitted


def test_the_verification_method_tokens_are_the_emitted_ones() -> None:
    """F4: the skill names only tokens the code emits, and the emitted strategy
    names are read from the strategies themselves."""
    emitted = _emitted_verification_methods()
    assert "no_op_marker" in emitted and "dry_run" in emitted
    assert "screenshot_diff" in emitted and "ui_control_text" in emitted
    assert "interference_post_action" in emitted and "ensure_app_probe" in emitted
    flat = _flat()
    for token in (
        "screenshot_diff",
        "window_state",
        "ui_control_text",
        "no_op_marker",
        "dry_run",
        "ensure_app_probe",
        "interference_post_action",
    ):
        assert token in emitted, f"the skill names {token!r} but the code never emits it"
        assert f"`{token}`" in flat
    # The invented tokens from master-mission prose never return.
    for invented in ("`identity`", "`pixels`", "`readback`"):
        assert invented not in flat, f"invented verification_method token {invented} returned"


def test_text_summary_exists_only_on_the_observation_tools() -> None:
    import computer_use_mcp.server.execute as execute_module

    # Code truth first: the execute surface never emits a text_summary key.
    assert "text_summary" not in inspect.getsource(execute_module)
    flat = _flat()
    assert "`text_summary` exists only on `computer_observe`/`computer_zoom` replies" in flat
    for line in _all_text().splitlines():
        if "text_summary" in line:
            assert "failure_reason" not in line, line
            assert "ExecutionResult" not in line, line
    assert "Read every other fact the reply carries: `text_summary`" not in flat


def test_the_single_image_scale_key_is_taught() -> None:
    from computer_use_mcp.server.replies import IMAGE_OUTBOUND_SCALE_KEY

    assert IMAGE_OUTBOUND_SCALE_KEY == "image_outbound_scale"
    flat = _flat()
    assert "`image_outbound_scale`" in flat
    assert "image_scale" not in flat.replace("image_outbound_scale", "")


def test_partial_delivery_is_taught_and_the_absolute_rejection_claim_is_gone() -> None:
    flat = _flat()
    assert "`partial_delivery`" in flat
    assert "`delivered_chars`" in flat
    assert "mid-type focus drift" in flat
    assert "A rejection dispatched nothing" not in flat
    assert "cannot double-apply" not in flat


def test_the_batch_stop_rule_sends_only_the_unexecuted_items() -> None:
    """F2: a stopped batch has already dispatched its earlier items."""
    execute_src = _read(SRC_DIR / "server" / "execute.py")
    assert "follow_up_results" in execute_src
    flat = _flat()
    assert "after a batch stop, send only the unexecuted items" in flat
    assert "re-send the identical FLAT call" in flat


def test_the_three_confidences_are_taught_as_separate_numbers() -> None:
    flat = _flat()
    assert "Three separate confidences" in flat
    assert "model, grounding, verification" in flat
    assert "`confidence` is not a success probability" in flat


# --- the enforced bounds, each expected string derived from the code value ------------------------


def test_the_hold_ms_band_and_family_are_stated_as_enforced() -> None:
    assert tuple(action.value for action in GroundedAction.HOLD_ACTIONS) == (
        "click",
        "right_click",
        "keypress",
        "hotkey",
    )
    assert _field_bound(GroundedAction, "hold_ms") == (0, MAX_HOLD_MS)
    flat = _flat()
    assert f"`hold_ms` (0..{MAX_HOLD_MS} ms, optional)" in flat
    assert "`click`/`right_click`/`keypress`/`hotkey`" in flat
    assert (
        "Neither `hold_ms` nor `duration_ms` is a top-level `computer_execute`" in flat
    )


def test_the_duration_ms_move_only_band_is_stated() -> None:
    assert _field_bound(GroundedAction, "duration_ms") == (0, MAX_DURATION_MS)
    assert f"`duration_ms` (0..{MAX_DURATION_MS} ms, `move` only)" in _flat()


def test_the_settle_band_is_stated() -> None:
    assert f"`settle_ms` (0..{MAX_PLAN_SETTLE_MS} ms)" in _flat()


def test_the_action_delta_band_is_enforced_and_never_taught_as_0_20() -> None:
    ge, le = _field_bound(GroundedAction, "delta")
    assert (ge, le) == (-20, 20)
    flat = _flat()
    assert f"{ge}..{le}" in flat
    assert "delta 0..20" not in flat
    assert "0..20 seconds" not in flat


def test_the_flat_wait_cap_is_ten_seconds_with_the_clamp_annotation() -> None:
    assert WAIT_MAX_SECONDS == 10.0
    flat = _flat()
    assert f"capped at {int(WAIT_MAX_SECONDS)} seconds" in flat
    assert f"`{WAIT_CLAMPED_MARKER}=<effective>`" in flat


def test_the_background_wait_ceiling_and_segment_are_stated() -> None:
    assert Limits().max_wait_seconds == 10800.0
    ceiling = f"{WAIT_HARD_MAX_SECONDS:,.0f}".replace(",", " ")
    flat = _flat()
    assert f"min(max_wait_seconds, {WAIT_HARD_MAX_SECONDS:.0f})" in flat
    assert f"{ceiling} s (3 h)" in flat
    assert f"default {WAIT_SEGMENT_DEFAULT_SECONDS:.0f} s" in flat


def test_the_wait_concurrency_and_tombstone_caps_are_stated() -> None:
    flat = _flat()
    assert f"{MAX_WAITS_PER_SESSION} per session and {MAX_WAITS_GLOBAL} process-wide" in flat
    assert "`too_many_waits`" in flat
    assert f"only the last {WAIT_TOMBSTONES_PER_SESSION} finished waits are remembered" in flat
    assert "`unknown_wait`" in flat
    assert "`already_cancelled:true`" in flat
    assert "`wait_failed`" in flat


def test_the_follow_ups_cap_is_stated() -> None:
    assert _field_bound(ActionSpec, "x") == (-8_192, 16_384)
    assert f"at most {MAX_FOLLOW_UPS}, ONLY" in _flat()


def test_the_plan_scale_and_timing_caps_are_stated() -> None:
    plan_ref = _reference("plan-protocol.md")
    plan_flat = _flat(plan_ref)
    assert f"at most {MAX_PLAN_POINTS} points" in plan_flat
    assert f"at most {MAX_PLAN_ACTIONS_PER_POINT} actions" in plan_flat
    assert f"`point_timeout_ms` (0..{MAX_PLAN_TIMEOUT_MS} ms)" in plan_flat
    assert f"`final_wait_ms` (0..{MAX_DURATION_MS} ms)" in plan_flat
    assert "not a substitute" in plan_flat
    assert f"1..{MAX_DURATION_MS} ms" in plan_flat
    assert f"(0..{int(WAIT_MAX_SECONDS)} s)" in plan_flat


def test_the_evidence_retention_bounds_are_stated() -> None:
    expected = (
        f"{EVIDENCE_MAX_FRAMES} frames / "
        f"{EVIDENCE_MAX_BYTES // (1024 * 1024)} MB / "
        f"{int(EVIDENCE_MAX_AGE_SECONDS)} s"
    )
    assert expected == "16 frames / 24 MB / 3600 s"
    assert expected in _flat()


def test_the_session_idle_timeout_default_is_stated() -> None:
    assert SESSION_IDLE_TIMEOUT_ENV == "CORTEX_SESSION_IDLE_TIMEOUT_SECONDS"
    flat = _flat()
    assert f"{int(SESSION_IDLE_TIMEOUT_DEFAULT_SECONDS / 60)} min" in flat
    assert f"`{SESSION_IDLE_TIMEOUT_ENV}`" in flat
    assert (
        f"band {SESSION_IDLE_TIMEOUT_MIN_SECONDS:.0f}..{SESSION_IDLE_TIMEOUT_MAX_SECONDS:.0f}, "
        "0 = disabled" in flat
    )
    assert "any tool call on the session resets it" in flat
    assert "a session with a running wait is never reaped" in flat


def test_the_session_limit_reply_lists_live_sessions() -> None:
    lifecycle_src = _read(SRC_DIR / "server" / "lifecycle.py")
    assert '"live_sessions"' in lifecycle_src
    assert '"idle_timeout_seconds"' in lifecycle_src
    flat = _flat()
    assert "`live_sessions`" in flat
    assert "`idle_timeout_seconds`" in flat
    assert "stop_session" in flat


def test_the_advertised_text_bounds_are_stated() -> None:
    text_len = _max_length(GroundedAction, "text")
    effect_len = _max_length(GroundedAction, "expected_effect")
    ge, le = _field_bound(Point, "x")
    flat = _flat()
    assert f"{ge}..{le}" in flat
    assert f"≤ {text_len} chars" in flat
    assert f"≤ {effect_len} chars" in flat


def test_every_remaining_number_the_skill_states_is_pinned_from_code() -> None:
    """N1 (VAL-WP9-02): restored. Pins the numbers no other node pins, each derived
    from the code: the 13-action enum, the wait-segment band, the 60 s client bound."""
    from computer_use_mcp.core.models import ActionType
    import computer_use_mcp.server.wait as wait_module

    assert len(ActionType) == 13
    assert f"Actions (exact names, {len(ActionType)})" in _core()
    assert f"{len(ActionType)}-action vocabulary" in _core()
    assert (
        f"band {WAIT_SEGMENT_MIN_SECONDS:.0f}..{WAIT_SEGMENT_MAX_SECONDS:.0f}" in _flat()
    )
    assert "60 s tool timeout" in inspect.getsource(wait_module)
    assert "60 s tool timeout" in _flat()


# --- behavioural truths the driver must be taught --------------------------------------------------


def test_the_cursor_rule_matches_plan_cursor_after() -> None:
    from computer_use_mcp.planning.plan import plan_cursor_after

    assert plan_cursor_after(3, 3, [{"kind": "rejected"}]) == 3
    assert plan_cursor_after(3, 3, [{"kind": "executed"}]) == 4
    flat = _flat()
    assert "`continue` runs the NEXT point" in flat
    assert "step refused before any dispatch keeps the cursor" in flat
    assert "retry is `plan_action=\"replace\"`" in flat


def test_the_policy_stop_acts_inside_one_step_only() -> None:
    """F5 (A3 K-53): on_uncertain/on_failure `stop` skips the rest of THAT POINT, the
    cursor still advances, and `continue` runs the next point."""
    agent_src = _read(SRC_DIR / "pipeline" / "agent.py")
    assert 'stopped_reason = "policy_stop"' in agent_src
    flat = _flat()
    assert "policy acts INSIDE one step only" in flat
    assert "the rest of THAT POINT is skipped" in flat
    assert "the cursor still advances past the attempted step" in flat
    assert "`continue` runs the NEXT point" in flat
    assert "halts the sequence" not in flat


def test_the_plan_lease_rules_name_the_refused_and_allowed_tools() -> None:
    import computer_use_mcp.server.fetch as fetch_module
    import computer_use_mcp.server.observe as observe_module
    import computer_use_mcp.server.wait as wait_module

    assert "_active_plan_tool_rejection" in inspect.getsource(observe_module)
    assert "_active_plan_tool_rejection" in inspect.getsource(wait_module)
    assert "_active_plan_tool_rejection" not in inspect.getsource(fetch_module)
    flat = _flat()
    assert "`plan_lease`" in flat
    assert "`computer_fetch_image` stays allowed during a lease" in flat
    assert "`computer_wait_status` stays allowed during a lease" in flat


def test_a_plan_with_continue_or_stop_under_a_lease_is_plan_lease_not_invalid() -> None:
    """F9: under a lease, plan + continue/stop is a plan_lease rejection; only
    replace accepts a plan (server/execute.py lease gate)."""
    execute_src = _read(SRC_DIR / "server" / "execute.py")
    assert "A replacement plan requires plan_action='replace'" in execute_src
    flat = _flat()
    assert "a `plan` sent with `continue`/`stop`" in flat
    assert "only `replace` accepts a plan" in flat
    assert "`invalid_plan_action`" in flat
    assert "executes the replacement's first step in the same" in flat


def test_the_plan_approval_rule_is_stated() -> None:
    flat = _flat()
    assert "decided once on the first plan call" in flat
    assert "`replace` inherits it" in flat
    assert "`continue` never re-asks" in flat


def test_the_plan_resubmit_rule_is_scoped_to_the_unexecuted_points() -> None:
    """F1 BLOCKER: a resubmitted plan starts a FRESH lease (plan_surface.py), so
    resubmitting the whole plan re-runs the executed prefix."""
    plan_surface_src = _read(SRC_DIR / "server" / "plan_surface.py")
    assert "new_lease = PlanLease(" in plan_surface_src
    assert "approved=lease.approved" in plan_surface_src
    flat = _flat()
    assert "ONLY the points not yet executed" in flat
    assert "never the executed prefix" in flat
    assert "starts a fresh lease and re-runs whatever it contains" in flat
    for line in _all_text().splitlines():
        if "resubmit" in line.casefold():
            assert (
                "only" in line.casefold()
                or "fresh lease" in line.casefold()
                or "repeat" in line.casefold()
            ), f"unscoped resubmit advice: {line!r}"


def test_a_frame_is_retained_only_when_the_reply_carries_the_image() -> None:
    """F3 (A3 K-75 replacement): retention happens at the ONE site where the reply is
    about to carry the image (execute.py), with attached=True on every producer; a
    reply without an image retained nothing."""
    execute_src = _read(SRC_DIR / "server" / "execute.py")
    assert "THIS reply is about to carry the post-action image" in execute_src
    for path in ("server/execute.py", "server/observe.py", "server/plan_surface.py"):
        source = _read(SRC_DIR / path)
        for match in re.finditer(r"register_frame\(", source):
            window = source[match.start() : match.start() + 400]
            assert "attached=True" in window, f"{path}: a register_frame call without attached=True"
    flat = _flat()
    assert "the reply then carries `image_id`" in flat
    assert "retained NOTHING for that call" in flat
    assert "retains NO frame" in flat
    assert "do not hunt for a fetchable id" in flat


def test_the_stated_effect_rule_keeps_pixel_only_changes_uncertain() -> None:
    # E-03: the verification engine's own doctrine states the pixel tier never claims
    # a described effect; the deterministic tiers are the verify path.
    from computer_use_mcp.pipeline import verification

    source = re.sub(r"\s+", " ", inspect.getsource(verification))
    assert "it never claims one" in source
    assert "the deterministic tiers are the verify path" in source
    flat = _flat()
    assert "satisfied only by a pixel change still stays `uncertain`" in flat
    assert "a stated `expected_effect`" in flat
    assert "the deterministic tiers are the verify path" in flat
    assert "or the pixel ladder" not in flat


def test_the_checkable_effect_guidance_is_taught() -> None:
    """F11: the driver must be told what a checkable stated effect looks like."""
    flat = _flat()
    assert "State a CHECKABLE effect" in flat
    assert "window" in flat and "process state" in flat and "UI control's text" in flat
    assert "not \"the screen changes\"" in flat


def test_the_half_resolution_and_view_parameters_are_taught() -> None:
    """F11: execute images default half-size; the visual-view parameters exist."""
    execute_src = _read(SRC_DIR / "server" / "execute.py")
    assert "half-res JPEG" in execute_src or "HALF-SIZE" in execute_src or "full_resolution" in execute_src
    observe_src = _read(SRC_DIR / "server" / "observe.py")
    assert "visual_view_density" in observe_src and "pixel_evidence" in observe_src
    flat = _flat()
    assert "HALF-SIZE image" in flat
    assert "`include_screenshot_after=true`" in flat
    assert "`visual_view`" in flat and "`visual_view_density`" in flat and "`pixel_evidence`" in flat
    assert "`region=[left, top, width, height]`" in flat


def test_the_focus_window_and_move_requirements_are_taught() -> None:
    models_src = _read(SRC_DIR / "core" / "models.py")
    assert "focus_window actions require a non-empty target window title" in models_src
    assert "move actions require a point (screenshot space)" in models_src
    flat = _flat()
    assert "`focus_window` needs a" in flat
    assert "window-title `target`" in flat
    assert "the point-bearing clicks need `x`/`y`" in flat


def test_markers_and_dry_run_are_taught_with_their_contract_tokens() -> None:
    import computer_use_mcp.server.lifecycle as lifecycle
    from computer_use_mcp.pipeline.agent import _NO_OP_MARKER_CONFIDENCE

    assert inspect.signature(lifecycle.start_session).parameters["dry_run"].default is False
    flat = _flat()
    assert "`result: \"marker\"`" in flat
    assert "`verification_method: no_op_marker`" in flat
    assert f"confidence {_NO_OP_MARKER_CONFIDENCE}" in flat
    assert "`done` does not end the session" in flat
    assert "`result: \"dry_run\"`" in flat
    assert "`dry_run` defaults to false" in flat


def test_the_scroll_sign_and_cursor_dependence_are_taught() -> None:
    flat = _flat()
    assert "POSITIVE = UP" in flat
    assert "CURRENT CURSOR POSITION" in flat


def test_the_integrity_statuses_are_taught_with_the_mask_caveat() -> None:
    """F14 (A3 K-88 residual): mismatch demotes to uncertain — a masked/transformed
    field may legitimately differ — and TextIntegrityError is named."""
    agent_src = _read(SRC_DIR / "pipeline" / "agent.py")
    assert "masked or transformed field" in agent_src
    assert 'outcome="uncertain"' in agent_src
    flat = _flat()
    for status in ("verified", "partial", "unverified", "mismatch"):
        assert f"`integrity={status}`" in flat, f"integrity status {status} is not taught"
    assert "masked or transformed field" in flat
    assert "demoted to `uncertain`" in flat
    assert "TextIntegrityError" in flat
    assert "Only `integrity=mismatch`" not in flat
    assert "confirms the text drop" not in flat


def test_the_launch_truth_is_taught_without_allowlist_overclaim() -> None:
    from computer_use_mcp.desktop.interference import _default_launch_policy
    import computer_use_mcp.pipeline.agent as agent_module

    assert _default_launch_policy() == "server"
    assert "target process is not in the configured process allowlist" in inspect.getsource(
        agent_module
    )
    flat = _flat()
    assert 'launch="server"' in flat
    assert "no process allowlist is configured" in flat
    assert "any resolvable target may spawn" in flat
    assert "CORTEX_ATTACH_OR_LAUNCH=driver" in flat
    assert "the server never launches by default" not in flat
    assert "never launches by default" not in flat


def test_the_clipboard_abort_path_limitation_is_disclosed() -> None:
    backend_src = _read(SRC_DIR / "desktop" / "backend.py")
    assert "ABORT PATH ONLY" in backend_src
    flat = _flat()
    assert "abort" in flat.casefold()
    assert "not disclosed" in flat
    assert 'via="clipboard"' in flat


def test_the_hotkey_chord_bounds_are_enforced_and_taught() -> None:
    with pytest.raises(ValueError) as excinfo:
        GroundedAction(action="hotkey", keys=["ctrl"])
    lo, hi = re.search(r"require (\d+) to (\d+)", str(excinfo.value)).groups()
    assert f"{lo}-{hi} key names" in _flat()


def test_the_session_lifecycle_rules_are_taught() -> None:
    flat = _flat()
    assert "`session_stopped`" in flat
    assert "`session_limit_exceeded`" in flat
    assert '`image_delivery="text"`' in flat
    assert "`max_steps`" in flat
    assert "`require_approval=false`" in flat


def test_the_evidence_discipline_separations_are_restored() -> None:
    """F13: the five separations (dev test :127-137) are stated in the core."""
    core = _core()
    for rule in (
        "Executing an action is not its effect taking place",
        "A changed screen is not a successful task",
        "`uncertain` is not failure",
        "A missing image is not a missing effect",
        "A confidence value is not visible proof",
    ):
        assert rule in core, f"the evidence-discipline section is missing: {rule!r}"


def test_the_glossary_interference_table_is_preserved() -> None:
    """F13 (dev glossary-block pin, adapted): the four pre-dispatch labels keep their
    table rows, in order, in the glossary reference."""
    glossary = _reference("glossary.md")
    rows = re.findall(r"^\|\s*`([a-z_]+)`\s*\|", glossary, re.MULTILINE)
    assert tuple(rows[:4]) == (
        "focus_taken_by",
        "focus_identity_unknown",
        "stuck_modifier",
        "target_gone",
    )


def test_the_post_dispatch_interference_events_are_taught_as_dispatched() -> None:
    """F7: FOCUS_DRIFTED / MODAL_DIALOG are post-action or mid-type — input WAS
    dispatched (agent.py interference_post_action demotion; mid-type partial_delivery)."""
    agent_src = _read(SRC_DIR / "pipeline" / "agent.py")
    assert 'verification_method="interference_post_action"' in agent_src
    assert "FOCUS_DRIFTED" in agent_src and "outcome=\"failed\"" in agent_src
    assert "partial_delivery=partial" in agent_src
    glossary = _reference("glossary.md")
    assert "Post-action / mid-type events" in glossary
    assert "input WAS" in glossary
    assert "`interference_post_action`" in glossary
    assert "may have landed" in glossary
    assert "The Interference Guard refuses an action BEFORE it dispatches" not in _flat()
    assert "no effect to judge" not in _flat()


# --- negative pins: the historical falsehoods must never return ------------------------------------


def test_the_false_sentences_from_val_wp9_are_absent() -> None:
    flat = _flat()
    for phrase in (
        # F3 / K-75: retention overclaims.
        "the capture is still retained",
        "image_attached:false",
        "text_session_no_image",
        "no image in a reply never means nothing was captured",
        "never the capture's evidence",
        # F4: invented verification_method tokens.
        "`identity`",
        "`pixels`",
        "`readback`",
        # F5: the wrong policy-stop semantics.
        "halts the sequence there",
        # F6/K-27: unconditional class mapping.
        "both report the class",
        # F9: the wrong plan+plan_action reply.
        "or a `plan` sent alongside",
        # F10b: the pixel ladder as a verify path.
        "or the pixel ladder",
        # F10c/K-16: approval as a rejection.
        "safety, approval, grounding",
        # F14/K-88: the overclaim.
        "Only `integrity=mismatch`",
        "confirms the text drop",
        # K-46/K-57/K-81, K-05, K-86, K-92, K-53, K-75 (originals).
        "`evidence` (`backend.executed`",
        "A rejection dispatched nothing",
        "cannot double-apply",
        "delta 0..20",
        "Read every other fact the reply carries",
        "no such field exists",
        "Every capture this session makes is KEPT",
        "Read `text_summary` first for economy",
    ):
        assert phrase not in flat, f"a false sentence returned: {phrase!r}"


def test_the_frontmatter_triggers_are_natural_and_accurate() -> None:
    head = _core().split("---", 2)[1]
    for name in ("start_session", "computer_execute", "computer_observe"):
        assert name in head
    for trigger in ("click", "type", "Windows app", "desktop"):
        assert trigger in head, f"the description lacks the natural trigger {trigger!r}"


def test_the_core_states_when_to_read_each_reference() -> None:
    skill = _core()
    for name, cue in (
        ("plan-protocol.md", "before submitting"),
        ("reply-contract.md", "reply shape"),
        ("glossary.md", "interference"),
        ("examples.md", "worked examples"),
    ):
        link_at = skill.rindex(f"references/{name}")
        window = skill[max(0, link_at - 200) : link_at + 200]
        assert cue in window, f"references/{name} has no when-to-read cue near its link"


def test_the_not_clear_list_names_every_live_interference_event_token() -> None:
    from computer_use_mcp.desktop import interference

    tokens = (
        interference.FOCUS_TAKEN_BY,
        interference.FOCUS_DRIFTED,
        interference.MODAL_DIALOG,
        interference.STUCK_MODIFIER,
        interference.FOCUS_IDENTITY_UNKNOWN,
        interference.TARGET_GONE,
    )
    flat = _flat()
    for token in tokens:
        assert f"`{token}`" in flat, f"the skill never names the interference event {token!r}"


def test_the_core_stays_within_the_line_budget() -> None:
    lines = _core().splitlines()
    assert len(lines) <= 250, (
        f"the core grew to {len(lines)} lines (budget 250); move detail into references/"
    )
