
from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

from ..core.limits import LimitExceeded
from ..core.models import ok_from_result
from ..core.state import TaskStopped
from ..pipeline.agent import rejection_reasons
from ..planning import plan as plan_layer
from ..planning.plan import PLAN_EVIDENCE_KEY, PlanLease, PlanLeaseGuard, plan_failure_reason
from ..vision.evidence import evidence_store, register_frame
from ..vision.visual_views import (
    VisualViewError,
    encode_png_b64,
    observation_for_payload,
)
from .pointer_reply import executed_pointer_fields
from .replies import (
    IMAGE_OUTBOUND_SCALE_KEY,
    _action_image_full,
    _execute_response_blocks,
    _redact_queue_entry,
    _redact_result_payload,
)
from .schemas import _teaching_invalid_action
from .state import IMAGE_DELIVERY_TEXT_NOTE



# --- WP7b: the waypoint plan surface (ORVEX-2026-101 Batch 2 / master-mission §1.2-§1.3) ------
# ONE normalization path, NO second executor: a plan normalizes to plain action-queue
# items (``[move, actions...]`` per point) and each STEP runs through the EXISTING
# queue via ``agent.run_single(..., plan_policies=..., plan_context=...)``, so every
# item inherits the full independent pipeline (ground -> validate -> safety ->
# approval -> execute -> verify) — "wait and re-observe" per point is inherited,
# never bypassed (RFC §4.1, decision D1). The data layer lives in plan.py; this
# section is ONLY the wire: the lease gate, one-step-per-call execution, the §1.3
# step/terminal replies, and the lease lifecycle.
#
# S-01: the dev copy's LEGACY single-call plan path (a whole plan executed in one
# call through the queue) is deliberately NOT ported — a plan is always stepwise
# here; a host that wants one-shot behavior passes a one-point plan.

#: The module-level lease guard (replaces the dev ``_PLANS_IN_FLIGHT`` set + the
#: ``bundle.extra`` bookkeeping helpers): atomic install, idempotent release — the
#: ONE exit for every terminal/session-end path.
_plan_guard = PlanLeaseGuard()


#: N1 (VAL-WP8-02): serializes the terminal-audit check-and-set across the
#: stop-session thread and the event loop (one terminal event per plan, always).
_plan_terminal_audit_lock = threading.Lock()


def _plan_counts(lease: PlanLease) -> dict[str, int]:
    """The ONE bounded count pair for every plan audit event (F4/VAL-WP8-01).

    Every emitter reads its counts here — no hand-summed copies, so the audit
    record's counts can never drift from the prefix the reply was built from.
    """
    return {
        "executed_count": sum(
            entry.get("kind") in {"executed", "dispatched_unverified"} for entry in lease.prefix
        ),
        "resolved_count": len(lease.prefix),
    }


def _audit_plan_event(
    bundle: Any,
    event_type: str,
    *,
    lease: PlanLease,
    result: str | None = None,
    **metadata: Any,
) -> None:
    """Emit one bounded plan lifecycle audit event (WP8) through the agent's sink.

    The redaction rules are the existing ones — the audit sink applies
    ``redact_text`` to every string field and metadata value, exactly as for every
    other event type; no plan-specific redaction exists.  Audit failures never
    break the plan path (the agent's ``_audit`` swallows them).  Every caller emits
    AFTER the reply's verdict fields are decided and reads ``result`` /
    ``failure_reason`` off the reply itself, so the audit record and the reply
    agree (E-05 rule).  Known sink convention (F4, documented in wp8-notes): the
    shared ``agent._audit`` drops ``None`` metadata values, so an explicit
    ``failure_reason: null`` (a verified step) is recorded as the key's ABSENCE —
    tests and consumers treat absent as null for plan events.
    """
    audit = getattr(getattr(bundle, "agent", None), "_audit", None)
    if audit is None:
        return
    audit(event_type, result=result, metadata={"plan_id": lease.context["plan_id"], **metadata})


def _audit_plan_terminal(
    bundle: Any,
    lease: PlanLease,
    *,
    reply_result: str,
    reason: str | None,
    failure_reason: str | None = None,
    **extra: Any,
) -> None:
    """Emit THE terminal lifecycle event of a plan — exactly once per lease.

    F3 (VAL-WP8-01): the terminal emission is IDEMPOTENT per lease — the first
    terminal path to reach it (an in-step TaskStopped handler or the session-end
    funnel racing it) wins, so a session killed mid-step can never produce a
    second terminal event for the same plan.

    F4 (VAL-WP8-01): ``reply_result`` is the RESULT TOKEN the terminal reply
    carries (verified / unknown / failed / stopped) — the event's ``result``
    equals the reply's, and ``plan_complete`` is emitted iff the plan verified.
    ``failure_reason`` is the reply's OWN failure_reason (null where the reply
    has none — absent ≡ null at the sink); ``reason`` is the lifecycle cause,
    consistent across paths: the reply's failure_reason where one exists
    (cancelled / invalid_action / execution_error / verification_*),
    ``completed`` for a verified plan, or the lifecycle cause no reply names
    (stop / replaced / exception).
    """
    # N1 (VAL-WP8-02): the check-and-set is ATOMIC under a dedicated lock — the
    # session-end funnel runs on a stop-session thread while the in-step
    # emitters run on the event loop, so a bare flag could double-emit.
    with _plan_terminal_audit_lock:
        if lease.terminal_audited:
            return
        lease.terminal_audited = True
    _audit_plan_event(
        bundle,
        "plan_complete" if reply_result == "verified" else "plan_stopped",
        lease=lease,
        result=reply_result,
        reason=reason or "",
        failure_reason=failure_reason,
        **_plan_counts(lease),
        **extra,
    )


#: Bounded retention for the lease's cross-call interference-event carry (D-3): the
#: guard produces at most a handful of events per step, but an unbounded lease is a
#: leak, so the retained list is deduped (order-preserving) and capped.
_PLAN_INTERFERENCE_EVENTS_MAX = 64

#: Bounded capture metadata per plan reply (the dev ``_plan_evidence`` bound).
_PLAN_EVIDENCE_CAPTURES_MAX = 32


def _plan_evidence(bundle: Any, lease: PlanLease) -> dict[str, Any]:
    """Return bounded backend/capture evidence WITHOUT fabricating observations.

    evidence-by-backend seam — WIRED (S-03 / decision D-2): the value is built
    from ``EvidenceStore.captures()`` (filled by the reply hooks, so it is real on
    BOTH backends) plus the lease's retained repositions — never from the fake
    backend's ``executed``/``observed`` attributes the dev copy read (those do not
    exist on ``LocalComputerBackend``; the dev block was fake-only). Served under
    :data:`PLAN_EVIDENCE_KEY` (``evidence_by_backend``) so ``evidence`` keeps meaning
    exactly one thing everywhere: a LIST of evidence lines.
    """
    store = evidence_store(bundle.extra)
    captures = store.captures() if store is not None else []
    return {
        "captures": captures[-_PLAN_EVIDENCE_CAPTURES_MAX:],
        "repositions": list(lease.repositions),
    }


def _active_plan_tool_rejection(bundle: Any) -> dict[str, Any] | None:
    """Reject an independent tool while a plan lease is waiting (typed ``plan_lease``).

    Read-only surfaces that CONSUME the lease's own state stay allowed:
    ``computer_wait_status`` (read-only, master-mission WP6 decision) and
    ``computer_fetch_image`` (S-04: a fetch is a READ-ONLY retrieval of evidence the
    session already holds). WP-R (ORVEX-2026-109): the recording tools
    ``computer_record`` / ``computer_record_result`` are ALLOWED during a lease
    too — they dispatch NO input and their capture worker is independent of the
    lease's grounding premise, and recording a plan's on-screen effect while
    the plan runs is a main use, so the record tools never consult this guard.
    Refused: flat computer_execute (its own gate inside
    computer_execute), computer_wait, computer_cancel_wait, and the independent
    observation tools computer_observe / computer_zoom / computer_screenshot (dev
    parity: a mid-plan independent observation would desynchronize the lease's
    grounding premise; recorded as the WP7b observe/zoom decision).
    """
    lease = _plan_guard.active_lease(bundle)
    if lease is None:
        return None
    return {
        "ok": False,
        "error": "plan_lease",
        "blocked": True,
        "message": "A stepwise plan lease is active; finish or stop the plan (continue/stop/replace) first.",
        "executed_prefix": [_redact_queue_entry(entry) for entry in lease.prefix],
        "remaining": plan_layer.plan_remaining(lease, "plan_lease_active"),
        PLAN_EVIDENCE_KEY: _plan_evidence(bundle, lease),
        "next_step": plan_layer.plan_next_step(lease),
    }


def _plan_lease_refusal(bundle: Any, lease: PlanLease, message: str) -> dict[str, Any]:
    """The typed zero-effect ``plan_lease`` refusal for a misdirected execute call.

    DELTA 1 #6 (§1.1): an execute-surface refusal is a REJECTED reply — it carries
    the closed ``result:"rejected"`` token and a ``reasons`` list naming the typed
    code, beside the kept ``error``/``blocked``/handshake keys (the validate_reply
    ``plan_refusal`` surface)."""
    return {
        "ok": False,
        "result": "rejected",
        "reasons": ["plan_lease"],
        "error": "plan_lease",
        "blocked": True,
        "message": message,
        "executed_prefix": [_redact_queue_entry(entry) for entry in lease.prefix],
        "remaining": plan_layer.plan_remaining(lease, "plan_lease_active"),
        PLAN_EVIDENCE_KEY: _plan_evidence(bundle, lease),
        "next_step": plan_layer.plan_next_step(lease),
    }


def _cutoff_budget_fields(
    bundle: Any,
    plan_context: dict[str, Any],
    *,
    limit_name: str | None,
    executed_count: int,
) -> dict[str, Any]:
    """Build the four truthful budget fields required at every cut."""
    required = int(plan_context.get("normalized_item_count") or 0)
    available = 0
    if limit_name == "max_actions":
        available = max(
            0,
            int(getattr(bundle.enforcer.limits, "max_actions", 0))
            - int(getattr(bundle.enforcer, "_actions", 0)),
        )
    elif limit_name == "max_steps":
        available = max(0, int(bundle.state.max_steps) - int(bundle.state.step_count))
    else:
        # Non-action gates still expose a bounded statement; zero is truthful when
        # the available budget is not a simple session counter.
        available = 0
    return {
        "limit_name": limit_name,
        "required": required,
        "available": available,
        "executed_count": int(executed_count),
    }


def _plan_limit_cutoff_from_outcome(
    outcome: Any,
    bundle: Any | None = None,
    plan_context: dict[str, Any] | None = None,
    prefix_entries: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Identify the established max_steps cut without introducing preflight policy."""
    if outcome.follow_ups_stopped_reason != "safety_denied":
        return None
    entries = list(prefix_entries if prefix_entries is not None else (outcome.follow_up_results or []))
    if not entries:
        return None
    final = list(outcome.follow_up_results or [entries[-1]])[-1]
    if (
        final.get("kind") == "safety_denied"
        and str(final.get("message", "")).startswith("Maximum session steps reached.")
        # DELTA 12 (Z3-a): the refusal now carries the restart teaching after
        # the leading sentence, so the cut is identified by its prefix.
    ):
        executed_count = sum(
            entry.get("kind") in {"executed", "dispatched_unverified"}
            for entry in entries
        )
        if bundle is not None and plan_context is not None:
            return {
                **_cutoff_budget_fields(
                    bundle, plan_context, limit_name="max_steps", executed_count=executed_count
                ),
                "resolved_count": len(entries),
            }
        return {
            "limit_name": "max_steps",
            "required": 0,
            "available": 0,
            "executed_count": executed_count,
            "resolved_count": len(entries),
        }
    return None


def _build_plan_aggregate(
    plan_context: dict[str, Any],
    outcome: Any | None = None,
    *,
    follow_up_results: list[dict[str, Any]] | None = None,
    stopped_reason: str | None = None,
    interference_events: list[str] | None = None,
    cutoff: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the per-point aggregate from the known queue prefix (§4.4/B4-8).

    Normal calls pass the first ``SingleActionOutcome`` unchanged.  Limit/cancel
    paths pass the mutable prefix retained by the plan context, never a fabricated
    remainder.  An entry's ``ok`` stays the queue's own verdict; a point is
    ``completed`` only when EVERY item is present and ok, while a known but
    incomplete prefix is explicitly ``partial``.  Redaction remains at this sink.
    The per-action rows echo ``hold_ms`` (E2) exactly as the queue entries carry it.
    """
    if outcome is not None:
        raw_entries = list(outcome.follow_up_results or [])
        stopped_reason = outcome.follow_ups_stopped_reason
        interference_events = outcome.interference_events
    else:
        raw_entries = list(follow_up_results or [])
    entries = [
        _redact_queue_entry(entry) if entry is not None else None
        for entry in raw_entries
    ]
    points_out: list[dict[str, Any]] = []
    last_entry_index = max(
        (index for index, entry in enumerate(entries) if entry is not None),
        default=-1,
    )
    for point in plan_context["points"]:
        point_entries = [
            entry
            for entry in entries[point["first_item"] : point["last_item"] + 1]
            if entry is not None
        ]
        if not point_entries:
            status = "skipped"
        else:
            all_ok = all(bool(entry.get("ok")) for entry in point_entries)
            complete_count = point["last_item"] - point["first_item"] + 1
            point_complete = len(point_entries) == complete_count
            has_failed = any(
                entry.get("verification_outcome") == "failed" or entry.get("kind") != "executed"
                for entry in point_entries
            )
            has_uncertain = any(
                entry.get("verification_outcome") == "uncertain" for entry in point_entries
            )
            has_dispatched_unknown = any(
                entry.get("kind") == "dispatched_unverified" for entry in point_entries
            )
            stop_landed_here = (
                stopped_reason is not None
                and point["first_item"] <= last_entry_index <= point["last_item"]
            )
            if all_ok and point_complete:
                status = "completed"
            elif all_ok and not point_complete:
                status = "partial"
            elif has_dispatched_unknown:
                status = "uncertain_stopped"
            elif has_failed:
                status = "failed"
            elif has_uncertain:
                status = "uncertain_stopped" if stop_landed_here else "uncertain"
            else:
                status = "failed"
        transition_entry = point_entries[0] if point_entries else None
        actions_out = [
            {
                "action": entry.get("action_type"),
                "hold_ms": entry.get("hold_ms"),
                "outcome": entry.get("kind"),
                "verification": entry.get("verification_outcome"),
                "ok": bool(entry.get("ok")),
            }
            for entry in point_entries[1:]
        ]
        points_out.append(
            {
                "id": point["id"],
                "index": point["index"],
                "status": status,
                "timeout_ms": point["timeout_ms"],
                "transition": (
                    {
                        "action": transition_entry.get("action_type"),
                        "duration_ms": transition_entry.get("duration_ms"),
                        "outcome": transition_entry.get("kind"),
                        "verification": transition_entry.get("verification_outcome"),
                        "ok": bool(transition_entry.get("ok")),
                    }
                    if transition_entry is not None
                    else None
                ),
                "actions": actions_out,
            }
        )
    final_wait_outcome: dict[str, Any] | None = None
    final_wait_item = plan_context["final_wait_item"]
    if final_wait_item is not None and len(entries) > final_wait_item:
        entry = entries[final_wait_item]
        # Sparse prefix (F1 fix 2.1): every item the plan did not reach is padded
        # with ``None`` (``_plan_aggregate_for_state``), and the trailing wait is
        # ALWAYS the last item — so it is ``None`` in exactly the case this
        # aggregate exists for: a response issued before the wait ran (any halted
        # step, ``plan_action="stop"``, ``plan_action="continue"``, the terminal
        # reply).  Apply the same ``is not None`` guard the point loop above
        # already uses and NAME the wait instead of raising out of the tool.
        final_wait_outcome = (
            {"outcome": "not_executed", "ok": False}
            if entry is None
            else {"outcome": entry.get("kind"), "ok": bool(entry.get("ok"))}
        )
    aggregate = {
        "plan_id": plan_context["plan_id"],
        "status": (
            "completed"
            if stopped_reason is None
            else "cancelled"
            if stopped_reason == "stopped"
            else "failed"
        ),
        "stopped_reason": stopped_reason,
        "points": points_out,
        "interference_events": list(interference_events or []),
        "final_wait_ms": plan_context["final_wait_ms"],
        **({"final_wait_outcome": final_wait_outcome} if final_wait_outcome is not None else {}),
    }
    if cutoff is not None:
        aggregate["cutoff"] = dict(cutoff)
    return aggregate


def _plan_aggregate_for_state(
    lease: PlanLease,
    *,
    status: str,
    stopped_reason: str | None,
    current_events: list[str] | None = None,
) -> dict[str, Any]:
    """Build the additive aggregate from the exact retained prefix.

    ``current_events`` is the step being built RIGHT NOW.  The lease is appended to
    further down in ``_plan_response`` (the only place the outcome and the lease are
    both in scope), i.e. AFTER this aggregate is built, so without this the reply for
    the very step that observed a guard event would under-report its own — a
    pre-dispatch rejection has no later call to wait for, because the point does not
    advance.
    """
    aligned: list[dict[str, Any] | None] = [None] * len(lease.specs)
    for entry, source_index in zip(lease.prefix, lease.source_indices, strict=False):
        if 0 <= source_index < len(aligned):
            aligned[source_index] = entry
    aggregate = _build_plan_aggregate(
        lease.context,
        follow_up_results=aligned,  # type: ignore[arg-type]
        stopped_reason=stopped_reason,
        # ORVEX-2026-105 D-3: the retained lease list is the only thing that can
        # carry a previous step's guard events — the aggregate is rebuilt from the
        # lease on every reply.
        interference_events=[*lease.interference_events, *(current_events or [])],
    )
    aggregate["status"] = status
    return aggregate


def _step_interference_events(outcome: Any) -> list[str]:
    """The step's merged interference events (DELTA 1 #3): EVERY queue item's events,
    collected by the queue into ``follow_up_interference_events``; falls back to the
    first outcome's own list for non-queue outcomes."""
    merged = getattr(outcome, "follow_up_interference_events", None)
    if merged is None:
        return list(outcome.interference_events or [])
    return list(merged)


def _plan_retain_events(lease: PlanLease, events: list[str] | None) -> None:
    """Dedupe + bound the lease's cross-call interference-event carry (D-3/WP3).

    Appended at the one site where the step outcome and the lease are simultaneously
    in scope; a later call (continue/stop/terminal) runs in a fresh frame and would
    otherwise report ``interference_events: []`` for a guard event the host was
    already told about in an earlier reply. Dedup preserves first-seen order; the
    oldest entries beyond the cap are dropped (bounded lease).
    """
    if not events:
        return
    seen = set(lease.interference_events)
    for event in events:
        if event not in seen:
            lease.interference_events.append(event)
            seen.add(event)
    del lease.interference_events[:-_PLAN_INTERFERENCE_EVENTS_MAX]


def _plan_carry_events(response: dict[str, Any], lease: PlanLease) -> None:
    """Mirror the lease's retained events onto a reply that owns no outcome of its own.

    ORVEX-2026-105 D-3: ``_plan_response`` publishes the STEP's events on its own
    top-level key, but the terminal / exception / walk-completed replies are built in
    a LATER call whose frame never saw a ``SingleActionOutcome`` — without this the
    events would survive only inside the nested aggregate.  Conditional on a
    non-empty lease so a plan that observed nothing keeps its exact pre-existing key
    set.
    """
    if lease.interference_events:
        response["interference_events"] = list(lease.interference_events)


def _plan_step_result(step_entries: list[dict[str, Any]]) -> str:
    """The STEP reply's ``result`` token (§1.3 plan-step surface).

    The terminal aggregate vocabulary names an all-uncertain step ``unknown``; the
    §1.3 step surface names that same state ``uncertain`` — one classifier
    (:func:`plan.plan_result_from_entries`), one mapping at the surface. Step
    replies never say ``unknown``; only a terminal plan reply can.
    """
    aggregate = plan_layer.plan_result_from_entries(step_entries)
    return "uncertain" if aggregate == "unknown" else aggregate


def _plan_not_executed(lease: PlanLease, reason: str) -> list[dict[str, Any]]:
    """Decision 3: the explicit, bounded list of plan work that never executed.

    Same shape and same boundedness as ``remaining`` (decision 6 additive).  The
    only difference is the per-step ``reason``, which carries the typed failure
    classification on EVERY entry — including the partially attempted current step,
    whose stored remainder still carries the generic historical phrase.  A verified
    step is not a failure, so it reports ``awaiting_plan_action``.
    """
    return plan_layer.plan_not_executed(lease, reason)


def _plan_terminal_response(
    bundle: Any,
    lease: PlanLease,
    *,
    result: str,
    reason: str,
) -> dict[str, Any]:
    """Return a terminal stop/replace handoff with no backend or capture effect."""
    aggregate = _plan_aggregate_for_state(
        lease, status="stopped" if reason == "stop" else "failed", stopped_reason=reason
    )
    executed_count = sum(
        entry.get("kind") in {"executed", "dispatched_unverified"} for entry in lease.prefix
    )
    cutoff = {
        **_cutoff_budget_fields(
            bundle, lease.context, limit_name="stop", executed_count=executed_count
        )
    }
    # The host closed this plan on purpose: ``stopped`` never verifies (V/4244 ok
    # rule), so ``ok`` is False regardless of how good the prefix looked.
    outcome = plan_layer.plan_terminal_outcome(
        lease.prefix, stopped=True, expected_items=int(lease.context["normalized_item_count"])
    )
    assert outcome.result == "stopped" and outcome.ok is False
    response: dict[str, Any] = {
        "ok": False,
        "result": result,
        "message": (
            "Plan stopped by the host; the executed prefix is preserved."
            if reason == "stop"
            else reason
        ),
        "blocked": False,
        "plan_id": lease.context["plan_id"],
        "executed_prefix": [_redact_queue_entry(entry) for entry in lease.prefix],
        "follow_up_results": [_redact_queue_entry(entry) for entry in lease.prefix],
        "remaining": plan_layer.plan_remaining(lease, reason),
        PLAN_EVIDENCE_KEY: _plan_evidence(bundle, lease),
        "cutoff": cutoff,
        "plan": aggregate,
        "step_index": max(lease.cursor - 1, 0),
        "next_step": None,
        # Batch 6 (decision 3, additive): the host closed this plan on purpose, so
        # nothing FAILED; the work it chose not to run is named by ``not_executed``.
        "failure_reason": None,
        "not_executed": _plan_not_executed(lease, reason),
    }
    response["plan"]["cutoff"] = dict(response["cutoff"])
    _plan_carry_events(response, lease)
    # WP8: the host closed the plan on purpose — the lifecycle ends as ``stopped``
    # (after the reply fields above are final, E-05); the reason names the
    # lifecycle cause ("stop"), the reply's failure_reason stays null.
    _audit_plan_terminal(
        bundle, lease, reply_result=str(response["result"]), reason=reason
    )
    return response


def _plan_exception_response(
    bundle: Any,
    lease: PlanLease,
    *,
    step_index: int,
    result: str,
    reason: str,
    blocked: bool,
    stopped: bool = False,
    failure_reason: str | None = None,
    limit_name: str | None = None,
) -> dict[str, Any]:
    """Return a truthful failed/unknown step without claiming unobserved work.

    F6 (VAL-WP8-01): ``limit_name`` comes from ``exc.limit_name`` at the handler —
    EVERY limit trip (not only ``max_actions``/``max_steps``) gets the agreeing
    ``error:"limit_exceeded"`` + ``limit`` + ``cutoff`` fields next to
    ``failure_reason:"limit_exceeded"``.
    """
    if failure_reason is None:
        failure_reason = (
            "cancelled"
            if stopped
            else ("limit_exceeded" if limit_name is not None else "execution_error")
        )
    response: dict[str, Any] = {
        "ok": False,
        "result": result,
        "message": reason,
        "blocked": blocked,
        "plan_id": lease.context["plan_id"],
        "step_index": step_index,
        "step_number": step_index + 1,
        "executed_prefix": [_redact_queue_entry(entry) for entry in lease.prefix],
        "follow_up_results": [_redact_queue_entry(entry) for entry in lease.prefix],
        "remaining": plan_layer.plan_remaining(lease, reason),
        PLAN_EVIDENCE_KEY: _plan_evidence(bundle, lease),
        "next_step": plan_layer.plan_next_step(lease) if blocked else None,
        "plan": _plan_aggregate_for_state(
            lease,
            status="blocked" if blocked else ("cancelled" if stopped else "failed"),
            stopped_reason=reason,
        ),
        "failure_reason": failure_reason,
        "not_executed": _plan_not_executed(lease, failure_reason),
    }
    if limit_name is not None:
        response["error"] = "limit_exceeded"
        response["limit"] = limit_name
    else:
        # Same literals the sibling reply builders already use for the very same
        # conditions; ``action_error`` matches ``outcome.kind == "error"`` exactly.
        response["error"] = {
            "execution_error": "action_error",
            "invalid_action": "invalid_action",
            "cancelled": "cancelled",
        }.get(failure_reason, "action_error")
    if limit_name is not None or stopped:
        cutoff_name = limit_name or "cancel"
        response["cutoff"] = {
            **_cutoff_budget_fields(
                bundle,
                lease.context,
                limit_name=cutoff_name,
                # F4: one count helper — the same executed_count every audit
                # event reads.
                executed_count=_plan_counts(lease)["executed_count"],
            ),
            "resolved_count": len(lease.prefix),
        }
        response["plan"]["cutoff"] = dict(response["cutoff"])
    if stopped:
        response["stopped"] = True
    _plan_carry_events(response, lease)
    return response


def _plan_response(
    bundle: Any,
    lease: PlanLease,
    outcome: Any,
    *,
    step_index: int,
    step_entries: list[dict[str, Any]],
    include_screenshot_after: bool | None,
    image_mode: str,
    view_provider: Any | None,
    complete_walk: bool = False,
    record_block: dict[str, Any] | None = None,
) -> Any:
    """Assemble the §1.3 plan-step reply around the per-item pipeline payload.

    One call == one waypoint step == one full pipeline pass (WP1 contract): the
    reply carries the step's ``result`` (the §1.3 plan-step token set), the typed
    ``failure_reason`` (ALWAYS, ``None`` on a verified step), ``not_executed``, the
    ``evidence_by_backend`` block (store captures + lease repositions, both
    backends), the executed prefix, the per-point aggregate (E2 ``hold_ms`` echo),
    and the step's interference events (D-3, retained on the lease).
    """
    if outcome.kind == "rejected":
        # §1.1: a gate refusal is a REJECTED reply — reasons, never an error key.
        response: dict[str, Any] = {
            "ok": False,
            "result": "rejected",
            "message": outcome.message or "Action rejected (no gate detail available).",
            "reasons": list(outcome.reasons),
        }
        if outcome.partial_delivery is not None:
            response["partial_delivery"] = dict(outcome.partial_delivery)
    elif outcome.kind == "safety_denied":
        response = {
            "ok": False,
            "result": "rejected",
            "message": outcome.message,
            "reasons": rejection_reasons(outcome),
        }
    elif outcome.kind == "approval_required":
        # §1.1 approval rule (O-03): ok=False, result="not_executed",
        # requires_approval=True — the SAME envelope as the flat approval gate.
        response = {
            "ok": False,
            "result": "not_executed",
            "requires_approval": True,
            "message": outcome.message,
        }
    elif outcome.kind == "digest_surprise":
        response = {
            "ok": False,
            "result": "rejected",
            "message": outcome.message,
            "reasons": rejection_reasons(outcome),
        }
    elif outcome.kind == "error":
        # §1.2: an operation error is ``failed``; the typed code stays.
        response = {"ok": False, "result": "failed", "error": "action_error", "message": outcome.message}
    else:
        result = outcome.result
        if result is None:
            response = {
                "ok": False,
                "result": "failed",
                "error": "action_error",
                "message": "Execution produced no result.",
            }
        else:
            response = _redact_result_payload(result.model_dump())
            response.update(
                {
                    "model_confidence": outcome.model_confidence,
                    "grounding_confidence": outcome.grounding_confidence,
                    "verification_confidence": outcome.verification_confidence,
                }
            )
    # The STEP verdict comes from the step's own entries through the ONE classifier
    # (never from the first item's token): rejected/approval outcomes name their own
    # §1.3 token directly.
    # DELTA 1 #5: a session killed mid-step (stop_session / idle reap / the internal
    # kill path) is a KILLED plan, not a finished one — the reply says ``stopped``
    # (ok false) even though the lease is already gone and the queue may have run.
    session_killed = bool(getattr(bundle.state, "stopped", False)) or bool(
        getattr(getattr(bundle.context, "stop", None), "stopped", False)
    )
    if outcome.kind == "approval_required":
        step_result = "not_executed"
    elif outcome.kind in {"rejected", "safety_denied", "digest_surprise"}:
        step_result = "rejected"
    elif outcome.kind == "error":
        step_result = "failed"
    elif session_killed:
        step_result = "stopped"
    else:
        step_result = _plan_step_result(step_entries)
        # §1.1 approval rule: when the step's queue was halted by an approval hold on
        # a LATER item (the first outcome is the executed transition move), the step
        # verdict is the approval token, not a failure — the point's action never
        # dispatched and approval is pending.
        if step_result == "failed" and any(
            entry.get("requires_approval") for entry in step_entries
        ):
            step_result = "not_executed"
    response["result"] = step_result
    response["ok"] = ok_from_result(step_result)
    if step_result == "not_executed":
        response["requires_approval"] = True
    response["follow_up_results"] = [_redact_queue_entry(entry) for entry in lease.prefix]
    response["follow_ups_stopped_reason"] = outcome.follow_ups_stopped_reason
    cutoff = _plan_limit_cutoff_from_outcome(
        outcome, bundle, lease.context, prefix_entries=lease.prefix
    )
    if cutoff is not None:
        response["cutoff"] = dict(cutoff)
        response["error"] = "limit_exceeded"
        response["limit"] = cutoff["limit_name"]
    response["executed_prefix"] = [_redact_queue_entry(entry) for entry in lease.prefix]
    response["step_index"] = step_index
    response["step_number"] = step_index + 1
    response[PLAN_EVIDENCE_KEY] = _plan_evidence(bundle, lease)
    response["next_step"] = plan_layer.plan_next_step(lease)
    response["remaining"] = plan_layer.plan_remaining(
        lease,
        "awaiting_plan_action" if response["result"] != "failed" else "previous_step_failed",
    )
    step_failure_reason = plan_failure_reason(
        outcome, step_entries, result=response["result"], error=response.get("error")
    )
    response["failure_reason"] = step_failure_reason
    response["not_executed"] = _plan_not_executed(
        lease, step_failure_reason or "awaiting_plan_action"
    )
    lease_active = _plan_guard.active_lease(bundle) is lease
    response["blocked"] = lease_active and not session_killed
    response["plan_id"] = lease.context["plan_id"]
    if lease.replaced_plan_id is not None:
        # WP7b M2 (VAL-WP7b-03): the replace disclosure rides the PAYLOAD (before the
        # content blocks are built), so an image-bearing replace reply — a
        # content-block LIST — keeps it too, exactly like a plain-dict reply.
        response["replaced_plan_id"] = lease.replaced_plan_id
        response["replaced_prefix"] = list(lease.replaced_prefix or [])
    response["message"] = response.get("message") or (
        f"Plan step {step_index + 1} executed; the lease awaits continue/stop/replace."
        if lease_active
        else f"Plan step {step_index + 1} executed."
    )
    if session_killed:
        # DELTA 1 #5: the session ended mid-step — the aggregate must not read as a
        # finished plan either.  F2 (VAL-WP8-01): the failure_reason is
        # UNCONDITIONALLY "cancelled" — the kill is the cause whatever the
        # classifier saw, and an entry-derived token must not survive it.
        response["message"] = (
            "The session was stopped while the plan step was in flight; the plan "
            "did not finish."
        )
        response["stopped"] = True
        response["failure_reason"] = "cancelled"
    response["plan"] = _plan_aggregate_for_state(
        lease,
        status=(
            "cancelled"
            if session_killed
            else "blocked"
            if lease_active
            else "completed"
        ),
        stopped_reason=(
            "stopped"
            if session_killed
            else "awaiting_plan_action"
            if lease_active
            else None
        ),
        # DELTA 1 #3: the step's events are the MERGED per-item list (every queue
        # item), not only the first outcome's.
        current_events=_step_interference_events(outcome),
    )
    # WP-E E1: the execute call's ``record`` block rides the step reply payload.
    if record_block is not None:
        response["record"] = record_block
    if cutoff is not None:
        response["plan"]["cutoff"] = dict(cutoff)
        response["plan"]["stopped_reason"] = "limit_exceeded"
    # ORVEX-2026-105 D-3: publish the STEP's events on their own top-level key AND
    # retain them on the lease (deduped, bounded) — the only place the per-step
    # outcome and the lease are both in scope.
    _plan_retain_events(lease, _step_interference_events(outcome))
    if lease.interference_events:
        response["interference_events"] = list(lease.interference_events)
    if complete_walk:
        # DELTA 2 N1/N3: this step verified itself and completed the walk — the
        # verdict fields become the whole-plan aggregate's (released here, once);
        # everything else the step disclosed stays in the payload (the image tail
        # below still runs, so the walk-ending reply carries the frame + image_id).
        _apply_terminal_verdict_fields(bundle, lease, response)
    # WP8: one plan_point audit event per step reply, emitted AFTER the verdict
    # fields are final (E-05: the metadata is read off the reply itself, so the
    # audit record and the reply can never disagree).  A walk-ending step ALSO
    # closes the lifecycle — with the SAME result token and the SAME reason
    # formula the terminal-walk reply uses (F4).
    _audit_plan_event(
        bundle,
        "plan_point",
        lease=lease,
        result=str(response["result"]),
        step_index=response["step_index"],
        step_number=response["step_number"],
        failure_reason=response.get("failure_reason"),
        **_plan_counts(lease),
    )
    if session_killed:
        # N4 (VAL-WP8-02): the funnel deferred to this in-flight step — the killed
        # step's own reply IS the terminal shape, so the lifecycle closes right
        # after its plan_point record (point before terminal, final counts).
        _audit_plan_terminal(
            bundle,
            lease,
            reply_result=str(response["result"]),
            reason=response.get("failure_reason") or "cancelled",
            failure_reason=response.get("failure_reason"),
        )
    if complete_walk:
        _audit_plan_terminal(
            bundle,
            lease,
            reply_result=str(response["result"]),
            reason=(
                response.get("failure_reason")
                or ("completed" if response["result"] == "verified" else "")
            ),
            failure_reason=response.get("failure_reason"),
        )
    # v07-007 R-21: non-executed shapes embed model-proposed text in ``message`` —
    # the SAME redaction the flat path applies.
    if outcome.kind in {"rejected", "safety_denied", "approval_required", "digest_surprise", "error"}:
        response = _redact_result_payload(response)
    if include_screenshot_after is False:
        # PERF-004 C4 opt-out: no image, honest None scale.
        if response.pop("screenshot_after_base64", None) is not None:
            response[IMAGE_OUTBOUND_SCALE_KEY] = None
    if outcome.kind != "executed" or "screenshot_after_base64" not in response:
        return response
    if image_mode == "text":
        # D1: text mode dominates — the attachment and bytes are removed and NOTHING
        # is retained for this call (no frame, no image_id: the retention site below
        # is never reached, matching the skill's retention truth).
        response.pop("screenshot_after_base64", None)
        response["image_delivery"] = "text"
        response[IMAGE_OUTBOUND_SCALE_KEY] = None
        response["image_delivery_note"] = IMAGE_DELIVERY_TEXT_NOTE
        return response
    frame = getattr(outcome.result, "_frame", None)
    full_resolution = include_screenshot_after is True or _action_image_full()
    view_id: str | None = None
    view_payload: tuple[str, Any] | None = None
    if view_provider is not None:
        view_id = view_provider.id
        if not view_provider.passthrough:
            try:
                derived = view_provider.derive(
                    observation_for_payload(str(response["screenshot_after_base64"]), frame)
                )
                started = time.perf_counter()
                encoded_b64 = encode_png_b64(derived.image)
                bundle.metrics.record_latency(
                    "view_derive_ms", (time.perf_counter() - started) * 1000.0
                )
                view_payload = (encoded_b64, derived.image)
            except VisualViewError as exc:  # fail-closed: no image, no raw fallback
                response.pop("screenshot_after_base64", None)
                response["visual_view"] = view_id
                response["visual_view_error"] = f"view_derive_failed: {exc}"
                return response
            except Exception as exc:  # noqa: BLE001 - fail-closed, no traceback
                response.pop("screenshot_after_base64", None)
                response["visual_view"] = view_id
                response["visual_view_error"] = (
                    f"view_derive_failed: {type(exc).__name__}: {exc}"
                )
                return response
    # WP5 retention rule: THIS reply carries the post-action image, so the frame is
    # retained HERE — the bytes kept are EXACTLY the bytes the reply carries (the
    # derived view when one was served, otherwise the raw capture), both pre-ladder.
    # DELTA 1 M7 / DELTA 2 N1+N2 (ORVEX-2026-109): the plan step's pointer uses the
    # SAME rule as the flat surface — ``executed_pointer_fields`` on the SHOWN
    # item.  WP-E E3: the shown item is the LAST executed one (its frame rides
    # the reply), so a step ending on a click shows Pressed at that click; a
    # step ending on a wait (or a rejected later item) shows Normal at the
    # captured cursor. ``last_click`` still reports the click from the ring.
    # There is no override and no show_pointer-independent branch: on a
    # show_pointer=False session the reply carries no pointer/last_click keys
    # and the stored frame keeps neither.
    shown_action = (
        outcome.image_after_action
        if outcome.image_after_action is not None
        else getattr(outcome.result, "action", None)
    )
    pointer_xy, pointer_state, pointer, pointer_fields = executed_pointer_fields(
        bundle, outcome.result, shown_action
    )
    carried_b64 = (
        str(view_payload[0]) if view_payload is not None else str(response["screenshot_after_base64"])
    )
    response.update(
        register_frame(
            bundle.extra,
            payload=carried_b64,
            source="computer_execute(plan_step)",
            attached=True,
            visual_view=view_id,
            pointer_x=pointer_xy[0] if pointer is not None else None,
            pointer_y=pointer_xy[1] if pointer is not None else None,
            pointer_state=pointer_state if pointer is not None else None,
            last_click=pointer_fields.get("last_click") if pointer_fields else None,
        )
    )
    if pointer_fields is not None:
        # DELTA 1 M7: the additive pointer/last_click keys ride the image-bearing
        # step reply (the in_image placeholder is fixed by the blocks builder).
        response.update(pointer_fields)
    # WP-E E3 / DELTA 1 m1: ``image_after_item`` attaches HERE — after the
    # text-mode return and the view-derive-failed returns above — so the key
    # rides ONLY step replies that really carry the image.  When the step ran
    # several items, the attached image is the LAST executed item's
    # post-action frame and the reply names it (always present on a
    # multi-item step's image-bearing reply, including the value 0 — m2).
    if outcome.image_after_item is not None:
        response["image_after_item"] = outcome.image_after_item
    # S-03: the evidence block is (re)built AFTER this reply's own frame was
    # retained, so ``captures`` includes the capture THIS reply carries — the same
    # truth the dev block reported from the backend's observation list.
    response[PLAN_EVIDENCE_KEY] = _plan_evidence(bundle, lease)
    return _execute_response_blocks(
        response,
        frame=frame,
        full_resolution=full_resolution,
        view_id=view_id,
        view_payload=view_payload,
        pointer=pointer,
    )


def _apply_terminal_verdict_fields(
    bundle: Any, lease: PlanLease, response: dict[str, Any]
) -> None:
    """Override a walk-ending STEP reply payload with the whole-plan aggregate
    verdict (in place, BEFORE the content blocks are built).

    DELTA 2 N1/N3: the reply keeps everything the completing step disclosed (image
    block + image_id, interference events, per-item fields like requires_approval /
    reasons / partial_delivery — ``_plan_response`` already retained the step's
    events on the lease and in the aggregate); ONLY the verdict fields are decided
    by ``plan_terminal_outcome`` over the whole retained prefix, so an earlier
    uncertain/failed point caps the reply at ``unknown``/``failed`` with
    ``ok:false`` even though the last step itself verified (the V/4244 fix). The
    lease is released exactly once here."""
    outcome = plan_layer.plan_terminal_outcome(
        lease.prefix,
        expected_items=int(lease.context["normalized_item_count"]),
    )
    result = outcome.result
    failure_reason = (
        None if result == "verified"
        else plan_failure_reason(None, lease.prefix, result=result)
    )
    _plan_guard.release(str(bundle.context.session_id), bundle)
    response["result"] = result
    response["ok"] = outcome.ok
    response["failure_reason"] = failure_reason
    response["message"] = (
        "Plan complete; every item verified."
        if result == "verified"
        else f"Plan walk finished; terminal verdict: {result}."
    )
    response["blocked"] = False
    response["next_step"] = None
    response["remaining"] = []
    response["not_executed"] = [] if result == "verified" else _plan_not_executed(lease, failure_reason)
    # WP7b nit (VAL-WP7b-03, decided): for a terminal ``unknown`` the nested
    # ``plan.status`` now reads ``"unknown"`` — the same token the top level uses —
    # instead of ``"blocked"``.  ``blocked`` claims the plan is parked awaiting the
    # host; a terminal walk is finished with nothing pending, and top-level
    # ``blocked`` is false here.  "unknown" (§1.2: at least one step uncertain, none
    # failed) is the honest aggregate status, and the nested status vocabulary gains
    # it as a documented terminal-only member.
    response["plan"]["status"] = (
        "completed" if result == "verified" else "failed" if result == "failed" else "unknown"
    )
    response["plan"]["stopped_reason"] = None if result == "verified" else failure_reason


def _plan_terminal_walk_reply(bundle: Any, lease: PlanLease) -> dict[str, Any]:
    """THE terminal reply of a finished plan walk (V/4244, VAL-WP7b-01 #1).

    Serves the ``continue`` path whose cursor is already past the last step (the
    parked-at-the-end shape of DELTA 2 N3: the last step did not verify itself, so
    it parked, and this is the host's explicit walk-end request). The final step's
    events and frame are already on the lease (retained by ``_plan_response`` when
    that step replied), so the aggregate over the retained prefix carries them.
    Releases the lease exactly once (the guard's release is idempotent)."""
    _plan_guard.release(str(bundle.context.session_id), bundle)
    # V/4244 (F2-0 honesty): the walk is over, but "the cursor passed the last
    # step" is not a success claim. The terminal verdict is read off the retained
    # prefix through plan.py's aggregate — ``ok`` is True ONLY for a fully-verified
    # plan (``ok = (result == "verified")`` by construction); unknown and stopped
    # are never success, and a partial prefix is capped at ``unknown`` by
    # ``expected_items``.
    outcome = plan_layer.plan_terminal_outcome(
        lease.prefix,
        expected_items=int(lease.context["normalized_item_count"]),
    )
    result = outcome.result
    # The typed classification rides the entries themselves (DELTA 1 #1 + F1,
    # VAL-WP8-01): with outcome=None the classifier reads the ENTRIES — an
    # approval-held prefix classifies ``approval_required``, a rejected follow-up
    # ``rejected``, a failed one ``verification_failed`` / ``execution_error``, an
    # uncertain one ``verification_uncertain`` — never a blanket token and never
    # ``unclassified`` for a prefix the entries can name.
    failure_reason = (
        None if result == "verified"
        else plan_failure_reason(None, lease.prefix, result=result)
    )
    # WP7b nit (VAL-WP7b-03, decided): the aggregate status of a terminal ``unknown``
    # is ``"unknown"`` — the same token the top level reports — never ``"blocked"``
    # (a finished walk parks nothing; top-level ``blocked`` is false here).
    aggregate_status = (
        "completed" if result == "verified" else "failed" if result == "failed" else "unknown"
    )
    aggregate_reason = None if result == "verified" else failure_reason
    completed: dict[str, Any] = {
        "ok": outcome.ok,
        "result": result,
        "message": (
            "Plan complete; every item verified."
            if result == "verified"
            else f"Plan walk finished; terminal verdict: {result}."
        ),
        "blocked": False,
        "plan_id": lease.context["plan_id"],
        "step_index": max(lease.cursor - 1, 0),
        "executed_prefix": [_redact_queue_entry(entry) for entry in lease.prefix],
        "follow_up_results": [_redact_queue_entry(entry) for entry in lease.prefix],
        "remaining": [],
        PLAN_EVIDENCE_KEY: _plan_evidence(bundle, lease),
        "next_step": None,
        "plan": _plan_aggregate_for_state(
            lease, status=aggregate_status, stopped_reason=aggregate_reason
        ),
        # A verified plan ran every declared item; a non-verified one names the work
        # that never produced evidence (a final step cut short by its own policy
        # stop, the final wait, ...).
        "failure_reason": failure_reason,
        "not_executed": [] if result == "verified" else _plan_not_executed(lease, failure_reason),
    }
    _plan_carry_events(completed, lease)
    # WP8: the terminal walk reply closes the lifecycle — result token identical
    # to the reply's, reason by the SAME formula the walk-ending step uses (F4).
    _audit_plan_terminal(
        bundle,
        lease,
        reply_result=result,
        reason=failure_reason or ("completed" if result == "verified" else ""),
        failure_reason=failure_reason,
    )
    return completed


async def _execute_plan_step(
    bundle: Any,
    lease: PlanLease,
    *,
    include_screenshot_after: bool | None,
    image_mode: str,
    view_provider: Any | None,
    record: Any | None = None,
    record_text_blocked: bool = False,
) -> Any:
    """Execute exactly one waypoint step and retain the lease unless terminal.

    WP-E E1: ``record`` (the band-validated record object accepted on the plan
    submission call and on ``plan_action="continue"``) starts the background
    recording JUST BEFORE the step's first input is dispatched (with the same
    0.3 s pre-roll); a launch problem NEVER refuses the step — the reply's
    ``record`` block reports {"started": false, reason} instead.  A text-mode
    session (``record_text_blocked``) reports the same without launching."""
    from .record import (
        RECORD_PRE_ROLL_SECONDS,
        _no_action_record_block,
        cancel_execute_recording,
        execute_record_input_dispatched,
        start_execute_recording,
    )

    session_id = str(bundle.context.session_id)
    if lease.cursor >= len(lease.steps):
        # DELTA 1 n7: a continue past the walk's end dispatches no input — a
        # valid ``record`` object is answered honestly with the closed
        # ``no_action`` reason instead of being silently ignored.
        reply = _plan_terminal_walk_reply(bundle, lease)
        if (record is not None or record_text_blocked) and isinstance(reply, dict):
            reply.setdefault("record", _no_action_record_block())
        return reply
    step_index = lease.cursor
    step_specs = lease.steps[step_index]
    lease.remainder = []
    step_context, policies = plan_layer.plan_step_context(lease, step_index, step_specs)
    try:
        grounded = step_specs[0].to_grounded(reason_prefix="MCP plan action")
        for spec in step_specs:
            spec.to_grounded(reason_prefix="MCP plan action")
    except Exception as exc:  # noqa: BLE001 - structural rejection is typed
        _plan_guard.release(session_id, bundle)
        # WP8: the structural rejection ends the lease — audit the stop (E-05: the
        # reply below carries result "failed" / failure_reason "invalid_action";
        # F4: the counts are the real prefix, which may already hold executed
        # entries from earlier steps).
        _audit_plan_terminal(
            bundle, lease, reply_result="failed", reason="invalid_action",
            failure_reason="invalid_action",
        )
        return _plan_exception_response(
            bundle,
            lease,
            step_index=step_index,
            result="failed",
            reason=f"Plan step could not be converted: {exc}",
            blocked=False,
            failure_reason="invalid_action",
        )
    # WP-E E1: the recording starts JUST BEFORE the step's first input is
    # dispatched (after the structural validation — a pre-dispatch structural
    # refusal never starts one).  A launch problem NEVER refuses the step; the
    # reply's ``record`` block reports it honestly.
    record_block: dict[str, Any] | None = None
    if record_text_blocked:
        from .record import _text_session_record_block

        record_block = _text_session_record_block()
    elif record is not None:
        record_block = start_execute_recording(bundle, session_id, record)

    def _with_record(reply: Any) -> Any:
        """WP-E E1: attach the ``record`` block to this step's reply (plain
        dict replies here; the block-carrying step replies get it via
        ``_plan_response``'s payload before serialization)."""
        if record_block is not None and isinstance(reply, dict):
            reply.setdefault("record", record_block)
        return reply

    # O-03: approval was evaluated ONCE on the FIRST plan call and is inherited
    # by every step and by replace — ``continue`` never re-asks.
    # DELTA 4 (VAL-WP8-04): ONE structure for the in-flight marker. The funnel
    # records ``kill_deferred`` (under the terminal-audit lock) instead of
    # emitting; the step's plan_point records are emitted INSIDE the guarded
    # region; the SINGLE ``finally`` clears the marker and closes the lifecycle
    # when a kill was deferred — on EVERY exit (including a raise in the merge),
    # after the merge and after the point, with the final prefix counts.
    lease.step_in_flight = True
    try:
        if record_block is not None and record_block.get("started"):
            # The pre-roll: part of the recording window (never added on top
            # of duration_s) — it captures the "before" state.  DELTA 1 m4:
            # INSIDE the guarded region, so a call that dies after the
            # recording started (CancelledError, any exception) cancels it
            # before propagating.
            await asyncio.sleep(RECORD_PRE_ROLL_SECONDS)
        outcome = await bundle.agent.run_single(
            bundle.state,
            grounded,
            approved=lease.approved,
            expected_effect=step_specs[0].expected_effect,
            follow_ups=step_specs[1:],
            follow_ups_cap=len(step_specs) - 1,
            plan_policies=policies,
            plan_context=step_context,
        )
    except TaskStopped as exc:
        step_entries = list(step_context.get("partial_results") or [])
        plan_layer.merge_plan_step_prefix(
            lease,
            step_entries,
            source_offset=int(lease.context["points"][step_index]["first_item"]),
        )
        plan_layer.set_plan_step_remainder(lease, step_index, step_specs, step_entries)
        # TaskStopped is the cancellation contract, including synthetic seams.
        if plan_layer.plan_step_attempted(step_entries):
            lease.cursor = plan_layer.plan_cursor_after(lease.cursor, step_index, step_entries)
        _plan_guard.release(session_id, bundle)
        # WP8: the session/stop-token was killed mid-step — the plan did NOT
        # finish; the lifecycle ends cancelled.  F3/F4: idempotent per lease (the
        # session-end funnel may have raced us here); the reason is the reply's
        # own failure_reason ("cancelled"), the result token is the reply's.
        # DELTA 4: the step's OWN plan_point record comes FIRST, with the final
        # prefix counts; the kill-deferred terminal (if any) is emitted by the
        # single finally AFTER this point.
        _audit_plan_event(
            bundle,
            "plan_point",
            lease=lease,
            result="stopped",
            step_index=step_index,
            step_number=step_index + 1,
            failure_reason="cancelled",
            **_plan_counts(lease),
        )
        _audit_plan_terminal(
            bundle, lease, reply_result="stopped", reason="cancelled",
            failure_reason="cancelled",
        )
        return _with_record(_plan_exception_response(
            bundle,
            lease,
            step_index=step_index,
            # DELTA 1 #5: the session/stop-token was killed mid-step — the plan was
            # NOT finished, so the reply says ``stopped`` (ok false by the terminal
            # rule), never a step verdict that could read as an honest completion.
            result="stopped",
            reason=str(exc),
            blocked=False,
            stopped=True,
            failure_reason="cancelled",
        ))
    except LimitExceeded as exc:
        step_entries = list(step_context.get("partial_results") or [])
        plan_layer.merge_plan_step_prefix(
            lease,
            step_entries,
            source_offset=int(lease.context["points"][step_index]["first_item"]),
        )
        plan_layer.set_plan_step_remainder(lease, step_index, step_specs, step_entries)
        if plan_layer.plan_step_attempted(step_entries):
            lease.cursor = plan_layer.plan_cursor_after(lease.cursor, step_index, step_entries)
        # WP8: the limit cut parks the lease (the plan continues on the host's
        # ``continue``) — this step still gets its own plan_point event. If the
        # session was killed mid-step (funnel deferred), the SINGLE finally below
        # closes the lifecycle — N-A (VAL-WP8-04) is structural now, not a
        # per-handler special case.
        _audit_plan_event(
            bundle,
            "plan_point",
            lease=lease,
            result="failed",
            step_index=step_index,
            step_number=step_index + 1,
            failure_reason="limit_exceeded",
            **_plan_counts(lease),
        )
        return _with_record(_plan_exception_response(
            bundle,
            lease,
            step_index=step_index,
            result="failed",
            reason=f"{exc.limit_name}: {exc}",
            blocked=True,
            failure_reason="limit_exceeded",
            # F6: EVERY limit carries its name — error / limit / cutoff agree with
            # failure_reason whatever limit tripped, not only the max_* ones.
            limit_name=exc.limit_name,
        ))
    except BaseException:
        # Structural/unexpected failures release the lease in this path; the outer
        # server teardown cannot accidentally leave a caller stuck behind a dead plan.
        # DELTA 2 NIT: the recording cancel runs FIRST — before the prefix merge
        # and the lease release — so a raise in either can never skip it.
        cancel_execute_recording(session_id, record_block)
        plan_layer.merge_plan_step_prefix(
            lease,
            list(step_context.get("partial_results") or []),
            source_offset=int(lease.context["points"][step_index]["first_item"]),
        )
        lease.cursor = step_index + 1
        _plan_guard.release(session_id, bundle)
        # F3/F4: idempotent per lease; "exception" is the lifecycle cause no step
        # reply names (the outer handler answers with a plain error envelope).
        _audit_plan_terminal(
            bundle, lease, reply_result="failed", reason="exception",
        )
        raise
    else:
        step_entries = list(outcome.follow_up_results or step_context.get("partial_results") or [])
        plan_layer.merge_plan_step_prefix(
            lease,
            step_entries,
            source_offset=int(lease.context["points"][step_index]["first_item"]),
        )
        plan_layer.set_plan_step_remainder(lease, step_index, step_specs, step_entries)
        # K-53: the cursor advances iff >= 1 item of the step dispatched input; a
        # pre-dispatch refusal (rejected/approval/safety) keeps the cursor so
        # ``continue`` re-executes the same step.
        lease.cursor = plan_layer.plan_cursor_after(lease.cursor, step_index, step_entries)
        # DELTA 2 n6: the plan step has the SAME zero-input detect-and-cancel as
        # the flat path — when the step was refused BEFORE any input (safety
        # denial, approval hold, digest surprise, or a rejection), the dispatch
        # ring between the recording's start and now is EMPTY (agent.py stamps
        # the ring at the ONE dispatch choke point before ``_backend_execute``),
        # so the cancel can never fire after input left the process.  The block
        # rides the step reply via ``_plan_response`` (same object).
        if (
            record_block is not None
            and record_block.get("started")
            and outcome.kind in {"safety_denied", "approval_required", "digest_surprise", "rejected"}
            and outcome.partial_delivery is None
            and not execute_record_input_dispatched(session_id, record_block)
        ):
            if cancel_execute_recording(session_id, record_block):
                record_block["cancelled"] = "action_refused"
        if outcome.kind == "error":
            # A backend/agent error is a structural failure, not a repairable step
            # verdict: release the lease in the guaranteed path so no caller is
            # stranded behind it.
            _plan_guard.release(session_id, bundle)
            # WP8: the lifecycle ends failed with the SAME reason the reply reports.
            _audit_plan_terminal(
                bundle, lease, reply_result="failed", reason="execution_error",
                failure_reason="execution_error",
            )
            return _with_record(_plan_exception_response(
                bundle,
                lease,
                step_index=step_index,
                result="failed",
                reason=outcome.message or "action_error",
                blocked=False,
                failure_reason="execution_error",
            ))
        # DELTA 1 #1 + DELTA 2 N3 (§1.3 "retry = replace"): the walk ends in THIS call
        # only when the LAST STEP ITSELF verified — and then the reply is the terminal
        # reply whose verdict comes from the whole-plan aggregate over the retained
        # prefix (an earlier uncertain/failed point still caps the verdict at
        # ``unknown``/``failed`` with ``ok:false`` — the #1 fix stays). A last step
        # that did NOT itself verify (uncertain / failed / rejected / partial) PARKS
        # the lease: the host may ``replace`` to retry or ``continue`` to receive the
        # terminal reply (built by the same aggregate over the whole prefix).
        session_killed = bool(getattr(bundle.state, "stopped", False)) or bool(
            getattr(getattr(bundle.context, "stop", None), "stopped", False)
        )
        if (
            lease.cursor >= len(lease.steps)
            and not session_killed
            and outcome.kind == "executed"
            and _plan_step_result(step_entries) == "verified"
        ):
            # N1 (VAL-WP7b-02): the terminal reply is the STEP reply (image registered
            # and attached, per-item disclosure kept, the step's events retained on the
            # lease and in the aggregate) with the verdict overridden by the whole-plan
            # aggregate — the completing step loses nothing.
            return _plan_response(
                bundle,
                lease,
                outcome,
                step_index=step_index,
                step_entries=step_entries,
                include_screenshot_after=include_screenshot_after,
                image_mode=image_mode,
                view_provider=view_provider,
                complete_walk=True,
                record_block=record_block,
            )
        return _plan_response(
            bundle,
            lease,
            outcome,
            step_index=step_index,
            step_entries=step_entries,
            include_screenshot_after=include_screenshot_after,
            image_mode=image_mode,
            view_provider=view_provider,
            record_block=record_block,
        )
    finally:
        # DELTA 4 (VAL-WP8-04): the ONE clear + deferred-kill close, on EVERY exit
        # (handlers, else-tail, a raise in the merge — N-B). Under the SAME lock
        # the funnel uses: the marker clears first, so a later funnel emits its
        # own (idempotent) terminal instead of re-deferring; a kill recorded
        # while the step ran closes the lifecycle HERE — after this step's
        # plan_point records (which are inside the guarded region) and with the
        # final prefix counts.
        with _plan_terminal_audit_lock:
            lease.step_in_flight = False
            deferred = lease.kill_deferred
            lease.kill_deferred = False
        if deferred:
            _audit_plan_terminal(
                bundle,
                lease,
                reply_result="stopped",
                reason="cancelled",
                failure_reason="cancelled",
            )


async def _handle_plan_action(
    bundle: Any,
    lease: PlanLease,
    *,
    plan_action: str | None,
    plan: Any,
    include_screenshot_after: bool | None,
    image_mode: str,
    view_provider: Any | None,
    record: Any | None = None,
    record_text_blocked: bool = False,
) -> Any:
    """Validate and service the additive continue/stop/replace handshake.

    WP-E E1: ``record``/``record_text_blocked`` ride through to the step
    execution (continue / replace) — the recording starts just before the
    step's input; a ``stop`` dispatches nothing and starts none."""
    session_id = str(bundle.context.session_id)
    if not isinstance(plan_action, str) or plan_action not in {"continue", "stop", "replace"}:
        return {
            "ok": False,
            "result": "rejected",
            "reasons": ["invalid_plan_action"],
            "error": "invalid_plan_action",
            "blocked": True,
            "plan_id": lease.context["plan_id"],
            "message": "plan_action must be continue, stop, or replace while a plan lease is active.",
            "executed_prefix": [_redact_queue_entry(entry) for entry in lease.prefix],
            "remaining": plan_layer.plan_remaining(lease, "awaiting_plan_action"),
            PLAN_EVIDENCE_KEY: _plan_evidence(bundle, lease),
            "next_step": plan_layer.plan_next_step(lease),
        }
    if plan_action == "stop":
        _plan_guard.release(session_id, bundle)
        # DELTA 1 n7: a stop dispatches no input — a valid ``record`` object is
        # answered honestly with the closed ``no_action`` reason (never
        # silently ignored, never started).
        stop_reply = _plan_terminal_response(bundle, lease, result="stopped", reason="stop")
        if (record is not None or record_text_blocked) and isinstance(stop_reply, dict):
            from .record import _no_action_record_block

            stop_reply.setdefault("record", _no_action_record_block())
        return stop_reply
    if plan_action == "replace":
        if plan is None:
            return {
                "ok": False,
                "result": "rejected",
                "reasons": ["invalid_plan_action"],
                "error": "invalid_plan_action",
                "blocked": True,
                "plan_id": lease.context["plan_id"],
                "message": "plan_action='replace' requires a complete replacement plan.",
                "executed_prefix": [_redact_queue_entry(entry) for entry in lease.prefix],
                "remaining": plan_layer.plan_remaining(lease, "awaiting_plan_action"),
                PLAN_EVIDENCE_KEY: _plan_evidence(bundle, lease),
                "next_step": plan_layer.plan_next_step(lease),
            }
        try:
            replacement = plan_layer.coerce_tolerant_plan(plan)
            replacement_specs, replacement_context = plan_layer.normalize_plan(replacement)
            for spec in replacement_specs:
                spec.to_grounded(reason_prefix="MCP plan action")
        except Exception as exc:  # noqa: BLE001 - malformed replacement has no effect
            return _teaching_invalid_action(exc, "done")
        old_id = lease.context["plan_id"]
        old_prefix = [_redact_queue_entry(entry) for entry in lease.prefix]
        _plan_guard.release(session_id, bundle)
        # WP8: the replaced plan's lifecycle ends as ``stopped`` (lifecycle reason
        # "replaced") BEFORE the replacement starts its own lifecycle.
        _audit_plan_terminal(bundle, lease, reply_result="stopped", reason="replaced")
        # O-03: approval was decided on the FIRST plan call; a replace INHERITS it.
        # The replace call's own ``approved`` parameter is deliberately NOT read —
        # an unapproved plan cannot be escalated into an approved one by replacing.
        new_lease = PlanLease(
            model=replacement,
            specs=replacement_specs,
            context=replacement_context,
            steps=plan_layer.plan_step_slices(replacement_specs, replacement_context),
            approved=lease.approved,
            replaced_plan_id=old_id,
            replaced_prefix=old_prefix,
        )
        _plan_guard.install(bundle, new_lease)
        # WP8: the replacement starts its own lifecycle (with the replace
        # disclosure, matching the reply the step call below returns).
        _audit_plan_event(
            bundle,
            "plan_start",
            lease=new_lease,
            result="started",
            point_count=replacement_context["point_count"],
            action_types=[spec.action.value for spec in replacement_specs],
            on_failure=replacement.on_failure,
            on_uncertain=replacement.on_uncertain,
            transition_duration_ms=replacement_specs[0].duration_ms,
            final_wait_ms=replacement_context["final_wait_ms"],
            replaced_plan_id=old_id,
        )
        response = await _execute_plan_step(
            bundle,
            new_lease,
            include_screenshot_after=include_screenshot_after,
            image_mode=image_mode,
            view_provider=view_provider,
            record=record,
            record_text_blocked=record_text_blocked,
        )
        # F11 (VAL-WP8-01): the replace disclosure rides ONLY the replace call's
        # own reply. The dict arm covers exception-shaped replies (the payload
        # builder attached the keys via the lease); afterwards BOTH leases'
        # disclosure fields are cleared so no later step of the replacement (or
        # of any further replace) re-carries the old plan's prefix.
        if isinstance(response, dict):
            response["replaced_plan_id"] = old_id
            response["replaced_prefix"] = old_prefix
        new_lease.replaced_plan_id = None
        new_lease.replaced_prefix = None
        return response
    return await _execute_plan_step(
        bundle,
        lease,
        include_screenshot_after=include_screenshot_after,
        image_mode=image_mode,
        view_provider=view_provider,
        record=record,
        record_text_blocked=record_text_blocked,
    )

