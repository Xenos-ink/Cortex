"""Batch-8 permanent guard: the failure-classification tables are COMPLETE.

Ported from the dev tree (``test_batch8_classification_completeness.py``),
adapted to the stable tree: the tables and the classifier live in
``computer_use_mcp.plan`` and the step entries carry the unified reply
contract's ``result`` token alongside ``verification_outcome``.

What this module pins:

* the literal universe is EXTRACTED from the real source with ``ast`` — every
  ``SingleActionOutcome(kind=...)`` literal, every value of the
  ``_interference_stop_reason`` prefix map, every literal assigned to
  ``stopped_reason``, every ``follow_ups_stopped_reason=`` keyword, the queue's
  ``outcome.kind if ... else "verification_failed"`` expression, and every
  constant ``error`` value the step/exception reply builders can emit;
* ``every_reachable_stop_reason_is_classified`` and
  ``no_table_key_is_unreachable`` keep the closure bidirectional;
* ``unclassified`` stays a reachable, documented fallback — pinned too.

Port scope note: the dev file's section (D/D2/D3) — the four E8 interference
labels' production leg over the fake backend's probe surface and the four
documentation glossary regions — is dev-audit-specific (ORVEX-2026-102 Phase 6
deliverables against the dev docs); the stable tree's interference-label
behavior is covered by its own focus/hotkey-guard and T8 tests, and the doc
glossaries do not exist here. The classification leg those sections added is
fully covered by the parametrized named-interference-stop test below.

Only ScriptedBackend and a scripted provider are used: no physical input, no
network.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest
from test_controller_integration import (
    FAST_LIMITS,
    ScriptedBackend,
    execute_payload,
    make_session,
)

from computer_use_mcp.pipeline import agent as agent_module
from computer_use_mcp.planning import plan as plan_module
import computer_use_mcp.server.lifecycle as srv_server_lifecycle
import computer_use_mcp.server.wait_machine as srv_server_wait_machine
import computer_use_mcp.server.wait as srv_server_wait
import computer_use_mcp.server.plan_surface as srv_server_plan_surface
import computer_use_mcp.server.execute as srv_server_execute
import computer_use_mcp.planning.plan as srv_planning_plan
from computer_use_mcp.desktop.focus_guard import GuardVerdict
from computer_use_mcp.core.models import FailureClass, WindowInfo
from computer_use_mcp.planning.plan import PLAN_FAILURE_REASONS, plan_failure_reason
from computer_use_mcp.core.state import SessionRegistry


def wire_plan(plan):
    from plan_explicitness import complete_plan

    return complete_plan(plan)


_PACKAGE_DIR = Path(agent_module.__file__).resolve().parent

_SERVER_PKG_DIR = _PACKAGE_DIR.parent / "server"


def _parse(name: str) -> ast.Module:
    return ast.parse((_PACKAGE_DIR / name).read_text(encoding="utf-8"))


def _parse_server_sources() -> list[ast.Module]:
    """Every module of the split ``server/`` package, sorted (WP-R2b step-1).

    The old monolith ``../server.py`` is now a package; the literal universe is
    the UNION over all of its modules, so nothing that moved can drop out."""
    return [
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for path in sorted(_SERVER_PKG_DIR.glob("*.py"))
    ]


# --- (A) the literal universe, extracted from the real source ------------------------------


def _outcome_kind_literals() -> set[str]:
    """Every ``SingleActionOutcome(kind="...")`` literal the product can construct."""
    found: set[str] = set()
    for node in ast.walk(_parse("agent.py")):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "SingleActionOutcome":
            for keyword in node.keywords:
                if keyword.arg == "kind" and isinstance(keyword.value, ast.Constant):
                    assert isinstance(keyword.value.value, str)
                    found.add(keyword.value.value)
    return found


def _interference_stop_literals() -> set[str]:
    """Every named interference stop ``_interference_stop_reason`` can return."""
    found: set[str] = set()
    for node in ast.walk(_parse("agent.py")):
        if isinstance(node, ast.FunctionDef) and node.name == "_interference_stop_reason":
            for inner in ast.walk(node):
                if not isinstance(inner, ast.Assign):
                    continue
                if not any(getattr(t, "id", None) == "prefixes" for t in inner.targets):
                    continue
                assert isinstance(inner.value, ast.Dict)
                for value in inner.value.values:
                    assert isinstance(value, ast.Constant) and isinstance(value.value, str)
                    found.add(value.value)
    return found


def _queue_stop_reason_sites() -> tuple[set[str], set[str]]:
    """The two indirect sites the queue uses to build a stop reason."""
    assigned: set[str] = set()
    for node in ast.walk(_parse("agent.py")):
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
            and any(
                isinstance(t, ast.Name) and t.id in {"stopped_reason", "reason"}
                for t in node.targets
            )
        ):
            assigned.add(node.value.value)
    keyword: set[str] = set()
    for node in ast.walk(_parse("agent.py")):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "SingleActionOutcome":
            for kw in node.keywords:
                if kw.arg == "follow_ups_stopped_reason" and isinstance(kw.value, ast.Constant):
                    assert isinstance(kw.value.value, str)
                    keyword.add(kw.value.value)
    return assigned, keyword


def _queue_kind_fallback_literal() -> set[str]:
    """``stopped_reason = ... or (outcome.kind if ... else "verification_failed")``."""
    found: set[str] = set()
    for node in ast.walk(_parse("agent.py")):
        if (
            isinstance(node, ast.IfExp)
            and isinstance(node.orelse, ast.Constant)
            and isinstance(node.orelse.value, str)
            and "outcome.kind" in ast.unparse(node.test)
        ):
            found.add(node.orelse.value)
    return found


def _reply_error_literals() -> set[str]:
    """Every constant ``error`` value the step/exception reply builders can emit.

    Batch-8 stable adaptation: the exception reply builder
    (``_plan_exception_response``) ALSO carries the typed ``error`` key (its
    ``cancelled``/``invalid_action``/``action_error`` mapping), so the closure
    scans both builders, not ``_plan_response`` alone.
    """
    found: set[str] = set()
    for tree in _parse_server_sources():
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.FunctionDef)
                and node.name in ("_plan_response", "_plan_exception_response")
            ):
                continue
            for inner in ast.walk(node):
                if isinstance(inner, ast.Assign) and len(inner.targets) == 1:
                    target = inner.targets[0]
                    if (
                        isinstance(target, ast.Subscript)
                        and isinstance(target.slice, ast.Constant)
                        and target.slice.value == "error"
                        and isinstance(inner.value, ast.Constant)
                        and isinstance(inner.value.value, str)
                    ):
                        found.add(inner.value.value)
                if isinstance(inner, ast.Assign) and isinstance(inner.value, ast.Dict):
                    for key, value in zip(inner.value.keys, inner.value.values):
                        if (
                            isinstance(key, ast.Constant)
                            and key.value == "error"
                            and isinstance(value, ast.Constant)
                            and isinstance(value.value, str)
                        ):
                            found.add(value.value)
    return found


def _failure_reason_literals() -> set[str]:
    """F9/N2 (VAL-WP8-01/02): the failure/stop literals OUTSIDE agent.py.

    Every string constant the server and plan modules bind to a failure_reason
    — by NAME assignment, SUBSCRIPT assignment (``response["failure_reason"] =
    "..."``), DICT-LITERAL entry (``{"failure_reason": "..."}``), the
    ``failure_reason=`` keyword, or the branches of an IfExp in any of those
    positions — is part of the classification universe: a literal
    the closed vocabulary does not name must fail here, not only new ``agent.py``
    queue literals.
    """
    found: set[str] = set()

    def _harvest(node: ast.expr) -> None:
        """Collect the str constants an assigned/passed expression can yield."""
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            found.add(node.value)
        elif isinstance(node, ast.IfExp):
            _harvest(node.body)
            _harvest(node.orelse)

    for tree in [*_parse_server_sources(), _parse("../planning/plan.py")]:
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                is_failure_target = any(
                    (isinstance(t, ast.Name) and t.id == "failure_reason")
                    or (
                        isinstance(t, ast.Subscript)
                        and isinstance(t.slice, ast.Constant)
                        and t.slice.value == "failure_reason"
                    )
                    for t in node.targets
                )
                if is_failure_target:
                    _harvest(node.value)
                if isinstance(node.value, ast.Dict):
                    for key, value in zip(node.value.keys, node.value.values):
                        if (
                            isinstance(key, ast.Constant)
                            and key.value == "failure_reason"
                        ):
                            _harvest(value)
            elif isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values):
                    if isinstance(key, ast.Constant) and key.value == "failure_reason":
                        _harvest(value)
            elif isinstance(node, ast.Call):
                for kw in node.keywords:
                    # ``stopped_reason=`` kwargs carry the AGGREGATE status
                    # vocabulary ("stopped"/"awaiting_plan_action"/...), not the
                    # failure_reason classes — only failure_reason is harvested.
                    if kw.arg == "failure_reason":
                        _harvest(kw.value)
    return found


CODE_KINDS = _outcome_kind_literals()
CODE_INTERFERENCE_STOPS = _interference_stop_literals()
_ASSIGNED, _KEYWORD = _queue_stop_reason_sites()
CODE_KIND_FALLBACK = _queue_kind_fallback_literal()
CODE_ERROR_KEYS = _reply_error_literals()
CODE_OTHER_SITE_LITERALS = _failure_reason_literals()

# Every literal that can actually reach ``SingleActionOutcome.follow_ups_stopped_reason``.
# ``"executed"`` is excluded on purpose: the queue only copies a NON-executed kind
# into a stop reason.
CODE_STOP_REASONS = (
    (CODE_KINDS - {"executed"})
    | CODE_INTERFERENCE_STOPS
    | _ASSIGNED
    | _KEYWORD
    | CODE_KIND_FALLBACK
)

#: Frozen mirror of ``CODE_STOP_REASONS`` (stable universe: the queue's
#: ``ensure_app_failed`` guard stop and the ``limit_exceeded`` cut are
#: stable-only members the dev list did not carry).
EXPECTED_STOP_REASONS = frozenset(
    {
        # the queue copies a non-executed outcome KIND verbatim
        "rejected",
        "safety_denied",
        "approval_required",
        "digest_surprise",
        "error",
        # the queue's own "an executed item with no result" literal
        "verification_failed",
        # the queue's own local literals
        "invalid_follow_up",
        "policy_stop",
        "no_post_action_observation",
        "limit_exceeded",
        "ensure_app_failed",
        # the six NAMED interference stops
        "modal_dialog",
        "focus_drifted",
        "focus_taken_by",
        "focus_identity_unknown",
        "stuck_modifier",
        "target_gone",
        # WP13 A: the per-call time budget ran out mid-chain — the executed items
        # keep their entries, the rest were never dispatched (classified
        # verification_uncertain).
        "time_budget",
    }
)

#: ``"executed"`` is deliberately NOT a failure: a verified step reports ``None``
#: and a non-verified executed step is named by the per-entry verdicts below the
#: kind table.
DOCUMENTED_UNNAMED_KINDS = frozenset({"executed"})

STOP_TABLES = (
    plan_module._PLAN_SPECIFIC_STOP_REASON_TO_FAILURE_REASON,
    plan_module._PLAN_STOP_REASON_TO_FAILURE_REASON,
)


# --- fixtures --------------------------------------------------------------------------------


@pytest.fixture
def fresh_server(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Any:
    monkeypatch.setenv("COMPUTER_USE_MCP_LOG_DIR", str(tmp_path / "audit"))
    monkeypatch.setattr(srv_server_lifecycle, "_registry", SessionRegistry(max_sessions=8))
    monkeypatch.setattr(srv_server_lifecycle, "_bundles", {})
    monkeypatch.setattr(srv_server_wait_machine, "_bundles", srv_server_lifecycle._bundles)
    monkeypatch.setattr(srv_server_wait, "_bundles", srv_server_lifecycle._bundles)
    monkeypatch.setattr(srv_server_plan_surface, "_plan_guard", srv_planning_plan.PlanLeaseGuard())
    monkeypatch.setattr(srv_server_execute, "_plan_guard", srv_server_plan_surface._plan_guard)
    monkeypatch.setattr(srv_server_lifecycle, "_plan_guard", srv_server_plan_surface._plan_guard)
    yield


TWO_ITEM_PLAN: dict[str, Any] = {
    "points": [
        {
            "id": "probe",
            "x": 100,
            "y": 100,
            "actions": [
                {"action": "click"},
                {"action": "click", "expected_effect": "the probe click landed"},
            ],
        },
        {
            "id": "next",
            "x": 300,
            "y": 300,
            "actions": [{"action": "click", "expected_effect": "the next point landed"}],
        },
    ]
}


class _StepOutcome:
    """The shape ``plan_failure_reason`` reads: first item executed, stop named."""

    def __init__(self, stopped_reason: str) -> None:
        self.kind = "executed"
        self.follow_ups_stopped_reason = stopped_reason


class FollowUpExplodesBackend(ScriptedBackend):
    """Land the first N backend dispatches, then raise on dispatch ``explode_on``."""

    def __init__(self, *, explode_on: int, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.explode_on = explode_on
        self.dispatches = 0

    def execute(self, action: Any, stop: Any = None, **kwargs: Any) -> str:
        from computer_use_mcp.core.models import GroundedAction

        self.dispatches += 1
        if self.dispatches == self.explode_on:
            raise RuntimeError("uv7 synthetic follow-up explosion")
        return super().execute(action, stop)


def _entry(**overrides: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "index": 0,
        "action_id": "a0",
        "action_type": "click",
        "kind": "executed",
        "result": "verified",
        "ok": True,
        "message": "",
        "reasons": [],
        "requires_approval": False,
    }
    entry.update(overrides)
    return entry


async def _run_step_with_grounding_fault(
    monkeypatch: pytest.MonkeyPatch, fault_message: str
) -> tuple[str, Any, dict[str, Any]]:
    """One plan step whose SECOND queue item is refused by the real grounding gate."""
    real_ground = agent_module.ComputerUseAgent._ground
    calls = {"n": 0}

    def counting_ground(self: Any, action: Any, observation: Any) -> Any:
        calls["n"] += 1
        # dispatch 1 = the in-plan move, 2 = the point's first action, 3 = its second
        if calls["n"] == 3:
            raise RuntimeError(fault_message)
        return real_ground(self, action, observation)

    monkeypatch.setattr(agent_module.ComputerUseAgent, "_ground", counting_ground)
    backend = ScriptedBackend(
        flip=True, active_window=WindowInfo(hwnd=1, pid=1, process_name="app.exe", title="App")
    )
    session_id, bundle, _backend, _provider = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    return session_id, bundle, execute_payload(
        await srv_server_execute.computer_execute(
            session_id, "done", plan=wire_plan(TWO_ITEM_PLAN), approved=True, include_screenshot_after=False
        )
    )


async def _run_step_with_guard_refusal(
    monkeypatch: pytest.MonkeyPatch, event: str, failure_class: Any
) -> dict[str, Any]:
    """One plan step whose SECOND queue item the real Interference Guard refuses."""
    real_pre_dispatch = agent_module.ComputerUseAgent._guard_pre_dispatch
    calls = {"n": 0}

    def counting_pre_dispatch(self: Any, action: Any) -> Any:
        calls["n"] += 1
        # dispatch 1 = the in-plan move, 2 = the point's first action, 3 = its second
        if calls["n"] == 3:
            return GuardVerdict(
                event=f"{event.upper()} title='Somewhere' process=other.exe hwnd=0",
                failure_class=failure_class,
                message=f"Interference guard refused the queued item: {event}",
                blocking=True,
                hints=["Re-ground and reattach before resuming."],
            )
        return real_pre_dispatch(self, action)

    monkeypatch.setattr(
        agent_module.ComputerUseAgent, "_guard_pre_dispatch", counting_pre_dispatch
    )
    backend = ScriptedBackend(
        flip=True, active_window=WindowInfo(hwnd=1, pid=1, process_name="app.exe", title="App")
    )
    session_id, _bundle, _backend, _provider = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    return execute_payload(
        await srv_server_execute.computer_execute(
            session_id, "done", plan=wire_plan(TWO_ITEM_PLAN), approved=True, include_screenshot_after=False
        )
    )


# --- (A) the guard: no reachable literal may be unnamed --------------------------------------


def test_code_stop_reason_universe_matches_the_frozen_list() -> None:
    """Drift detector: a new stop reason must be acknowledged here, explicitly."""
    missing = sorted(CODE_STOP_REASONS - EXPECTED_STOP_REASONS)
    stale = sorted(EXPECTED_STOP_REASONS - CODE_STOP_REASONS)
    assert not missing and not stale, (
        "the product's stop-reason universe changed. "
        f"new literals to classify: {missing}; "
        f"literals that no longer exist in the code: {stale}. "
        "Update EXPECTED_STOP_REASONS (with a comment) AND the classification table."
    )


def test_every_reachable_stop_reason_is_classified() -> None:
    """THE GUARD.  No literal the product can emit may be unnamed by both tables."""
    covered = set(STOP_TABLES[0]) | set(STOP_TABLES[1])
    uncovered = sorted(CODE_STOP_REASONS - covered)
    assert not uncovered, (
        f"stop reasons the classification tables do not name: {uncovered}. "
        "Add each one to _PLAN_SPECIFIC_STOP_REASON_TO_FAILURE_REASON (when it "
        "NAMES the cause and must win over the per-entry verdict) or to "
        "_PLAN_STOP_REASON_TO_FAILURE_REASON (otherwise) — otherwise a reachable "
        "state reports the unclassified escalation fallback."
    )


def test_no_reachable_stop_reason_reports_unclassified() -> None:
    """Drive the classifier for every literal in the shape that reaches it."""
    entries = [_entry(index=0), _entry(index=1)]
    unclassified = []
    for literal in sorted(CODE_STOP_REASONS):
        got = plan_failure_reason(
            _StepOutcome(literal), entries, result="failed", error=None
        )
        if got == "unclassified":
            unclassified.append(literal)
    assert not unclassified, f"these reachable stop reasons report unclassified: {unclassified}"


def test_every_outcome_kind_is_classified_or_documented() -> None:
    """The kind table must name every constructible kind except the documented ones."""
    kind_table = plan_module._PLAN_OUTCOME_KIND_TO_FAILURE_REASON
    uncovered = sorted(CODE_KINDS - set(kind_table) - DOCUMENTED_UNNAMED_KINDS)
    assert not uncovered, f"outcome kinds the kind table does not name: {uncovered}"
    assert CODE_KINDS - {"executed"} <= set(kind_table), (
        "every non-executed kind the queue can copy into a stop reason must also be "
        "named in the KIND table, so the two places the same literal appears agree"
    )
    assert "executed" not in kind_table


def test_every_response_error_literal_is_classified() -> None:
    """Every ``error`` value the step/exception builders can emit is named."""
    uncovered = sorted(CODE_ERROR_KEYS - set(plan_module._PLAN_ERROR_TO_FAILURE_REASON))
    assert not uncovered, f"typed error values the error table does not name: {uncovered}"


def test_every_classification_token_is_inside_the_closed_vocabulary() -> None:
    for table in (
        plan_module._PLAN_ERROR_TO_FAILURE_REASON,
        plan_module._PLAN_OUTCOME_KIND_TO_FAILURE_REASON,
        *STOP_TABLES,
    ):
        for key, value in table.items():
            assert value in PLAN_FAILURE_REASONS, (key, value)


def test_no_table_key_is_unreachable() -> None:
    """A table must not accumulate keys the product can never produce.

    N2 (VAL-WP8-02): each table is checked against its OWN emission source —
    the error table against the builders' ``error`` literals, the kind table
    against the constructible outcome kinds, the stop tables against the queue's
    stop-reason universe — so no cross-source union can let a dead key pass by
    coincidence.
    """
    sources: tuple[tuple[str, dict[str, str], set[str]], ...] = (
        ("_PLAN_ERROR_TO_FAILURE_REASON", plan_module._PLAN_ERROR_TO_FAILURE_REASON, CODE_ERROR_KEYS),
        ("_PLAN_OUTCOME_KIND_TO_FAILURE_REASON", plan_module._PLAN_OUTCOME_KIND_TO_FAILURE_REASON, CODE_KINDS),
        ("_PLAN_SPECIFIC_STOP_REASON_TO_FAILURE_REASON", STOP_TABLES[0], CODE_STOP_REASONS),
        ("_PLAN_STOP_REASON_TO_FAILURE_REASON", STOP_TABLES[1], CODE_STOP_REASONS),
    )
    for name, table, universe in sources:
        dead = sorted(set(table) - universe)
        assert not dead, f"{name} carries keys its emission source cannot produce: {dead}"
    # and the cross-site literals stay inside the closed vocabulary, each one
    # actually classifiable by SOME table (value-side, not key-side coincidence)
    assert CODE_OTHER_SITE_LITERALS, "the extraction found nothing — fix the test"
    uncovered = sorted(CODE_OTHER_SITE_LITERALS - PLAN_FAILURE_REASONS)
    assert not uncovered, f"failure literals outside the closed vocabulary: {uncovered}"


# --- (B) a follow-up refused WITHOUT a named interference stop -------------------------------


async def test_rejected_follow_up_is_not_unclassified(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A queue item refused by a gate that names no interference stop."""
    _session_id, _bundle, payload = await _run_step_with_grounding_fault(
        monkeypatch, "synthetic gate refusal with no named interference stop"
    )
    assert payload["follow_ups_stopped_reason"] == "rejected", payload
    assert [entry["kind"] for entry in payload["executed_prefix"]] == [
        "executed",
        "executed",
        "rejected",
    ], payload["executed_prefix"]

    assert payload["failure_reason"] != "unclassified", payload["failure_reason"]
    assert payload["failure_reason"] == "rejected", payload["failure_reason"]
    assert payload["failure_reason"] in PLAN_FAILURE_REASONS

    assert payload["result"] == "failed"
    assert payload["blocked"] is True
    assert payload["step_index"] == 0
    assert [entry["step_index"] for entry in payload["not_executed"]] == [1]
    assert {entry["reason"] for entry in payload["not_executed"]} == {"rejected"}
    assert [entry["reason"] for entry in payload["remaining"]] == ["previous_step_failed"]
    json.dumps(payload)


@pytest.mark.parametrize(
    ("event", "failure_class", "expected"),
    [
        ("modal_dialog", FailureClass.UNEXPECTED_DIALOG, "modal_dialog"),
        ("focus_drifted", FailureClass.WRONG_WINDOW, "focus_drifted"),
        ("focus_taken_by", FailureClass.WRONG_WINDOW, "focus_drifted"),
        ("focus_identity_unknown", FailureClass.WRONG_WINDOW, "focus_drifted"),
        ("stuck_modifier", FailureClass.BLOCKED_UI, "rejected"),
        ("target_gone", FailureClass.WRONG_WINDOW, "rejected"),
    ],
)
async def test_named_interference_stop_is_classified_not_unclassified(
    fresh_server: Any,
    monkeypatch: pytest.MonkeyPatch,
    event: str,
    failure_class: Any,
    expected: str,
) -> None:
    """Every named interference stop the driver protocol documents is classified."""
    payload = await _run_step_with_guard_refusal(monkeypatch, event, failure_class)
    assert payload["follow_ups_stopped_reason"] == event, payload
    assert payload["failure_reason"] == expected, payload
    assert payload["failure_reason"] != "unclassified"
    assert payload["failure_reason"] in PLAN_FAILURE_REASONS


# --- (C) scope guards: the fallback and everything that came before --------------------------


def test_unclassified_stays_the_documented_fallback() -> None:
    """A state the vocabulary genuinely does not name still escalates honestly."""
    assert "unclassified" in PLAN_FAILURE_REASONS

    class _Unnamed:
        kind = None
        follow_ups_stopped_reason = "a_stop_reason_the_product_never_emits"

    assert (
        plan_failure_reason(_Unnamed(), [], result="failed", error=None)
        == "unclassified"
    )


def test_every_own_outcome_kind_still_agrees_with_its_stop_reason() -> None:
    """The two places one literal appears must never disagree."""
    for kind in sorted(CODE_STOP_REASONS & CODE_KINDS):
        in_specific = kind in STOP_TABLES[0]
        in_generic = kind in STOP_TABLES[1]
        assert not (in_specific and in_generic), f"{kind} is named by both stop tables"
        classified_as_stop = (
            STOP_TABLES[0].get(kind) if in_specific else STOP_TABLES[1].get(kind)
        )
        assert classified_as_stop is not None, kind
        assert classified_as_stop == plan_module._PLAN_OUTCOME_KIND_TO_FAILURE_REASON[kind], (
            kind,
            plan_module._PLAN_OUTCOME_KIND_TO_FAILURE_REASON[kind],
            classified_as_stop,
        )


def test_pre_existing_classifications_and_vocabulary_are_unchanged() -> None:
    """Freeze: the dev Batch-6/7 entries keep their exact tokens (as a subset)."""
    assert plan_module._PLAN_ERROR_TO_FAILURE_REASON["action_error"] == "execution_error"
    assert plan_module._PLAN_ERROR_TO_FAILURE_REASON["limit_exceeded"] == "limit_exceeded"
    # F8: the unreachable error-table keys (invalid_action/cancelled/digest_surprise)
    # are gone — the exception builder passes those tokens explicitly.
    assert set(plan_module._PLAN_ERROR_TO_FAILURE_REASON) == {"limit_exceeded", "action_error"}
    assert plan_module._PLAN_OUTCOME_KIND_TO_FAILURE_REASON == {
        "safety_denied": "safety_denied",
        "approval_required": "approval_required",
        "digest_surprise": "digest_surprise",
        "error": "execution_error",
        "rejected": "rejected",
    }
    for key, value in {
        "verification_failed": "verification_failed",
        "invalid_follow_up": "invalid_follow_up",
        "policy_stop": "policy_stop",
        "no_post_action_observation": "no_post_action_observation",
        "error": "execution_error",
        "rejected": "rejected",
        "approval_required": "approval_required",
        "digest_surprise": "digest_surprise",
        "focus_taken_by": "focus_drifted",
        "focus_identity_unknown": "focus_drifted",
        "stuck_modifier": "rejected",
        "target_gone": "rejected",
    }.items():
        assert plan_module._PLAN_STOP_REASON_TO_FAILURE_REASON.get(key) == value, key
    for key, value in {
        "safety_denied": "safety_denied",
        "limit_exceeded": "limit_exceeded",
        "modal_dialog": "modal_dialog",
        "focus_drifted": "focus_drifted",
    }.items():
        assert plan_module._PLAN_SPECIFIC_STOP_REASON_TO_FAILURE_REASON.get(key) == value, key
    # F8: the unreachable "stopped" key is gone from the specific table.
    assert "stopped" not in plan_module._PLAN_SPECIFIC_STOP_REASON_TO_FAILURE_REASON
    assert len(PLAN_FAILURE_REASONS) == 16


def test_verified_step_still_reports_no_failure() -> None:
    class _Ok:
        kind = "executed"
        follow_ups_stopped_reason = None

    assert plan_failure_reason(_Ok(), [_entry()], result="verified", error=None) is None


def test_every_other_site_failure_literal_is_classified() -> None:
    """F9 (VAL-WP8-01): the failure literals server.py/plan.py assign are inside
    the closed vocabulary — a new literal outside agent.py fails here too."""
    assert CODE_OTHER_SITE_LITERALS, "the extraction found nothing — fix the test"
    uncovered = sorted(CODE_OTHER_SITE_LITERALS - PLAN_FAILURE_REASONS)
    assert not uncovered, f"failure literals outside the closed vocabulary: {uncovered}"


# --- (E) F1/F7 (VAL-WP8-01): terminal prefixes classify from the ENTRIES; an ---
# --- explicit stop reason outranks entry-derived uncertainty -------------------


async def test_terminal_walk_over_a_rejected_prefix_is_not_unclassified(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F1: a terminal reply (``continue`` past the end) classifies a REJECTED
    follow-up from the entries alone — never ``unclassified``."""
    session_id, bundle, payload = await _run_step_with_grounding_fault(
        monkeypatch, "synthetic gate refusal with no named interference stop"
    )
    assert payload["failure_reason"] == "rejected"
    assert payload["blocked"] is True
    # step 1 runs (uncertain, parks), then the explicit walk-end request
    await srv_server_execute.computer_execute(
        session_id, "done", plan_action="continue", include_screenshot_after=False
    )
    terminal = execute_payload(
        await srv_server_execute.computer_execute(
            session_id, "done", plan_action="continue", include_screenshot_after=False
        )
    )
    assert terminal["result"] == "failed"  # a rejected entry FAILS the plan (§1.2)
    assert terminal["failure_reason"] == "rejected", terminal["failure_reason"]
    assert terminal["failure_reason"] in PLAN_FAILURE_REASONS
    # F5 (VAL-WP8-02): the FAILED terminal-walk record agrees with the reply
    from test_audit_compliance import _plan_events

    events = _plan_events(bundle, session_id)
    assert events[-1]["event_type"] == "plan_stopped"
    assert events[-1]["result"] == terminal["result"]
    assert events[-1]["metadata"].get("failure_reason") == terminal["failure_reason"]
    assert events[-1]["metadata"]["resolved_count"] == len(terminal["executed_prefix"])


async def test_terminal_walk_over_an_approval_held_prefix_is_not_unclassified(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F1: the terminal reply of a prefix whose point's action is approval-held
    classifies ``approval_required`` from the entries' requires_approval marker."""
    backend = ScriptedBackend(
        flip=True, active_window=WindowInfo(hwnd=1, pid=1, process_name="app.exe", title="App")
    )
    session_id, _bundle, _backend, _provider = make_session(
        monkeypatch,
        backend=backend,
        dry_run=False,
        require_approval=True,
        limits=FAST_LIMITS,
    )
    step = execute_payload(
        await srv_server_execute.computer_execute(
            session_id,
            "done",
            plan=wire_plan(
                {
                    "transition_duration_ms": 0,
                    "points": [
                        {"x": 10, "y": 20, "actions": [{"action": "click", "expected_effect": "the click landed"}]}
                    ],
                }
            ),
            include_screenshot_after=False,
        )
    )
    assert step["result"] == "not_executed" and step["requires_approval"] is True
    assert step["failure_reason"] == "approval_required"
    terminal = execute_payload(
        await srv_server_execute.computer_execute(
            session_id, "done", plan_action="continue", include_screenshot_after=False
        )
    )
    assert terminal["failure_reason"] == "approval_required", terminal["failure_reason"]
    assert terminal["failure_reason"] in PLAN_FAILURE_REASONS


async def test_terminal_walk_over_an_error_prefix_is_not_unclassified(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F1: a terminal reply over a FAILED entry that carries no verification
    outcome (a dispatch error) classifies ``execution_error`` from the entry."""
    backend = FollowUpExplodesBackend(explode_on=2, flip=True)
    session_id, _bundle, _backend, _provider = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    step = execute_payload(
        await srv_server_execute.computer_execute(
            session_id,
            "done",
            plan=wire_plan(
                {
                    "transition_duration_ms": 0,
                    "points": [
                        {
                            "x": 10,
                            "y": 20,
                            "actions": [
                                {"action": "click"},
                                {"action": "click", "expected_effect": "the second click landed"},
                            ],
                        }
                    ],
                }
            ),
            approved=True,
            include_screenshot_after=False,
        )
    )
    assert step["failure_reason"] == "execution_error"
    assert step["blocked"] is True
    terminal = execute_payload(
        await srv_server_execute.computer_execute(
            session_id, "done", plan_action="continue", include_screenshot_after=False
        )
    )
    assert terminal["result"] == "failed"
    assert terminal["failure_reason"] == "execution_error", terminal["failure_reason"]
    assert terminal["failure_reason"] in PLAN_FAILURE_REASONS


async def test_an_uncertain_first_item_does_not_mask_the_stop_reason(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F7: an explicit queue stop reason (a named interference stop, a rejection)
    wins over entry-derived uncertainty from an EARLIER item."""
    uncertain_first_plan = {
        "transition_duration_ms": 0,
        "points": [
            {
                "x": 100,
                "y": 100,
                "actions": [
                    {"action": "click", "expected_effect": "the probe click landed"},
                    {"action": "click", "expected_effect": "the second click landed"},
                ],
            },
            {"x": 300, "y": 300, "actions": [{"action": "click", "expected_effect": "next"}]},
        ],
    }
    real_pre_dispatch = agent_module.ComputerUseAgent._guard_pre_dispatch
    calls = {"n": 0}

    def counting_pre_dispatch(self: Any, action: Any) -> Any:
        calls["n"] += 1
        # dispatch 1 = move, 2 = the uncertain first click, 3 = the refused second
        if calls["n"] == 3:
            return GuardVerdict(
                event="FOCUS_TAKEN_BY title='Somewhere' process=other.exe hwnd=0",
                failure_class=FailureClass.WRONG_WINDOW,
                message="Interference guard refused the queued item: focus_taken_by",
                blocking=True,
                hints=["Re-ground and reattach before resuming."],
            )
        return real_pre_dispatch(self, action)

    monkeypatch.setattr(
        agent_module.ComputerUseAgent, "_guard_pre_dispatch", counting_pre_dispatch
    )
    backend = ScriptedBackend(
        flip=True, active_window=WindowInfo(hwnd=1, pid=1, process_name="app.exe", title="App")
    )
    session_id, _bundle, _backend, _provider = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    payload = execute_payload(
        await srv_server_execute.computer_execute(
            session_id, "done", plan=wire_plan(uncertain_first_plan),
            approved=True, include_screenshot_after=False,
        )
    )
    assert payload["follow_ups_stopped_reason"] == "focus_taken_by", payload
    assert payload["failure_reason"] == "focus_drifted", payload["failure_reason"]
    assert payload["failure_reason"] != "verification_uncertain"
