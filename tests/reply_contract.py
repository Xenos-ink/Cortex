"""Shared reply-contract assertion helpers (ORVEX-2026-108 MASTER MISSION §1).

This module is the normative machine-readable form of MASTER MISSION §1 — the
UNIFIED REPLY CONTRACT: one envelope with identical semantics for every tool
reply, every ``follow_up_results[i]`` item, every plan step reply, the terminal
plan reply, and wait/cancel/fetch replies. No compatibility flags, no per-tool
or per-version variants.

It contains ONLY pure assertion helpers (no ``test_``-prefixed functions, no
fixtures, no ``computer_use_mcp`` imports), so pytest collects zero tests from
it. Later contract tests (WP1 ``tests/test_reply_contract.py`` and beyond)
import these helpers and drive real replies through them.

§1 rules encoded here:

* ok-rule (§1.1): ``ok is True`` **iff** ``result ∈ {verified, marker}``.
  Everything else — uncertain, failed, rejected, not_executed, dry_run,
  stopped, unknown, lifecycle refusal — is ``ok=False``.
* closed ``result`` token set (§1.2): exactly the nine tokens below; per-surface
  subsets per the §1.3 mapping table.
* presence rules per surface (§1.1/§1.3): ``ok`` + ``message`` on every reply;
  ``result`` on every action-bearing reply; ``verification`` on dispatched
  input actions; ``failure_reason`` + ``not_executed`` + ``evidence_by_backend``
  on plan-step and terminal replies (always, not only on failure); ``reasons``
  on rejected replies (never an ``error`` key).
* ``requires_approval`` rule (§1.1): approval-gated replies carry
  ``requires_approval: true`` with ``ok=False, result="not_executed"``;
  ``approval_required`` exists only as a ``failure_reason`` /
  ``follow_ups_stopped_reason`` VALUE, never as a reply KEY.
* ``partial_delivery`` shape (§1.1, K-05): on rejected replies where input may
  have partially landed — ``{"delivered_chars": N}`` with ``N >= 0``; absence
  means "nothing dispatched".
* single image scale key (§1.1, D-3): ``image_outbound_scale`` is the ONLY
  scale key on every image-bearing reply (observe/zoom/execute/fetch);
  ``image_scale`` is deleted.
"""

from __future__ import annotations

from typing import Any

# --- §1.2: the closed result token set ----------------------------------------------------

RESULT_TOKENS: frozenset[str] = frozenset(
    {
        "verified",    # declared effect established by fresh evidence        -> ok True
        "uncertain",   # dispatched, unproven; never silently upgraded         -> ok False
        "failed",      # proven not achieved, or operation error               -> ok False
        "rejected",    # a gate refused (zero effect, except partial delivery) -> ok False
        "not_executed",  # intended but never dispatched                       -> ok False
        "dry_run",     # validated, deliberately not dispatched                -> ok False
        "marker",      # intentional non-screen claim (done/wait)              -> ok True
        "stopped",     # host-requested plan stop; nothing verified            -> ok False
        "unknown",     # terminal plan aggregate: >=1 step uncertain, none failed -> ok False
    }
)

#: §1.1 ok-rule: the ONLY result tokens that may pair with ``ok is True``.
OK_TRUE_RESULTS: frozenset[str] = frozenset({"verified", "marker"})

#: §1.1 verification block: the outcome literals (same three everywhere).
VERIFICATION_OUTCOMES: frozenset[str] = frozenset({"verified", "uncertain", "failed"})

#: §1.1 ensure_app outcome enum (S-12 typed Literal).
ENSURE_APP_OUTCOMES: frozenset[str] = frozenset(
    {
        "reattached",
        "ambiguous",
        "not_found",
        "launched",
        "launch_rejected",
        "launch_unresolved",
    }
)

#: §1.3 per-surface allowed ``result`` subsets. ``stopped`` is included on the
#: execute surfaces for the host-requested stop reply (WP1: the TaskStopped reply
#: carries the §1.2 token; plan-terminal ``stopped`` lands in WP7).
SURFACE_RESULT_TOKENS: dict[str, frozenset[str]] = {
    "flat_execute": frozenset({"verified", "uncertain", "failed", "rejected", "dry_run", "marker", "stopped"}),
    "follow_up_item": frozenset({"verified", "uncertain", "failed", "rejected", "dry_run", "marker", "stopped"}),
    "plan_step": frozenset({"verified", "uncertain", "failed", "rejected", "not_executed"}),
    "terminal_plan": frozenset({"verified", "unknown", "failed", "stopped"}),
    "wait": frozenset({"marker"}),
    "cancel_wait": frozenset({"marker", "failed", "rejected"}),
    "wait_status": frozenset({"marker", "failed", "rejected"}),
    "fetch_image": frozenset({"verified", "rejected"}),
}

_IMAGE_BEARING_SURFACES: frozenset[str] = frozenset(
    {"observe", "zoom", "screenshot", "execute", "flat_execute", "fetch_image"}
)


# --- low-level helpers ---------------------------------------------------------------------


def assert_ok_rule(reply: dict[str, Any]) -> None:
    """§1.1 ok-rule: ``ok`` is True iff ``result`` is ``verified`` or ``marker``."""
    ok = reply.get("ok")
    result = reply.get("result")
    assert ok is True or ok is False, f"ok must be a bool, got {ok!r}"
    if ok:
        assert result in OK_TRUE_RESULTS, (
            f"ok-rule violated: ok=True requires result in {sorted(OK_TRUE_RESULTS)}, "
            f"got result={result!r}"
        )
    else:
        assert result != "verified" and result != "marker", (
            f"ok-rule violated: result={result!r} must carry ok=True"
        )


def assert_result_token(reply: dict[str, Any], surface: str | None = None) -> str:
    """§1.2 closed token set (+ §1.3 per-surface subset when a surface is named)."""
    result = reply.get("result")
    assert isinstance(result, str), f"result must be a str token, got {result!r}"
    assert result in RESULT_TOKENS, (
        f"result {result!r} outside the closed §1.2 token set {sorted(RESULT_TOKENS)}"
    )
    if surface is not None:
        allowed = SURFACE_RESULT_TOKENS.get(surface)
        assert allowed is not None, f"unknown surface {surface!r}"
        assert result in allowed, (
            f"surface {surface!r} allows result in {sorted(allowed)}, got {result!r}"
        )
    return result


def assert_envelope(reply: dict[str, Any], *, result_required: bool = True) -> None:
    """§1.1 presence rules shared by every reply: ok + message (+ result)."""
    assert "ok" in reply, "every reply carries ok"
    assert "message" in reply, "every reply carries a human-readable message"
    assert isinstance(reply["message"], str) and reply["message"], "message must be non-empty"
    if result_required:
        assert "result" in reply, "action-bearing reply carries result"
        assert_result_token(reply)


def assert_verification_block(
    reply: dict[str, Any], *, required: bool = True
) -> dict[str, Any] | None:
    """§1.1 verification: {outcome, confidence, evidence[], verification_method}.

    Present on dispatched input actions; ``outcome`` uses the same three
    literals everywhere (VERIFICATION_OUTCOMES).
    """
    verification = reply.get("verification")
    if verification is None:
        assert not required, (
            "dispatched input action reply must carry a verification block"
        )
        return None
    assert isinstance(verification, dict), f"verification must be a dict, got {verification!r}"
    outcome = verification.get("outcome")
    assert outcome in VERIFICATION_OUTCOMES, (
        f"verification.outcome {outcome!r} outside {sorted(VERIFICATION_OUTCOMES)}"
    )
    assert "confidence" in verification, "verification carries confidence"
    confidence = verification["confidence"]
    assert isinstance(confidence, (int, float)) and 0.0 <= confidence <= 1.0, (
        f"verification.confidence must be a number in [0, 1], got {confidence!r}"
    )
    evidence = verification.get("evidence")
    assert isinstance(evidence, list), f"verification.evidence must be a list, got {evidence!r}"
    method = verification.get("verification_method")
    assert isinstance(method, str) and method, (
        "verification.verification_method names the tier (dry_run, no_op_marker, identity, pixels, ...)"
    )
    return verification


# --- per-surface / per-situation helpers ---------------------------------------------------


def assert_rejection(reply: dict[str, Any]) -> None:
    """§1.1 rejected replies: {ok:False, result:'rejected', message, reasons}.

    No ``error`` key; ``reasons`` is a non-empty gate-named refusal list.
    """
    assert reply.get("result") == "rejected", (
        f"expected result='rejected', got {reply.get('result')!r}"
    )
    assert reply.get("ok") is False, "rejected replies are ok=False (§1.1 ok-rule)"
    reasons = reply.get("reasons")
    assert isinstance(reasons, list) and reasons, (
        "rejected replies carry a non-empty gate-named reasons list"
    )
    assert "error" not in reply, "rejected replies carry reasons, never an error key"
    assert_envelope(reply, result_required=False)


def assert_partial_delivery(reply: dict[str, Any]) -> None:
    """§1.1 K-05: partial_delivery = {'delivered_chars': N}, N int >= 0.

    Only meaningful on rejected replies where input may have partially landed
    (mid-type focus drift); absence of the key means "nothing dispatched".
    """
    assert reply.get("result") == "rejected" and reply.get("ok") is False, (
        "partial_delivery appears on rejected replies"
    )
    partial = reply.get("partial_delivery")
    assert isinstance(partial, dict), f"partial_delivery must be a dict, got {partial!r}"
    assert set(partial) >= {"delivered_chars"}, (
        "partial_delivery carries delivered_chars"
    )
    delivered = partial["delivered_chars"]
    assert isinstance(delivered, int) and not isinstance(delivered, bool) and delivered >= 0, (
        f"delivered_chars must be an int >= 0, got {delivered!r}"
    )


def assert_approval_gate(reply: dict[str, Any]) -> None:
    """§1.1 approval-gated reply: ok=False, result='not_executed',
    requires_approval=True; ``approval_required`` never appears as a KEY."""
    assert reply.get("ok") is False, "approval-gated reply is ok=False"
    assert reply.get("result") == "not_executed", (
        f"approval-gated reply result must be 'not_executed', got {reply.get('result')!r}"
    )
    assert reply.get("requires_approval") is True, (
        "approval-gated reply carries requires_approval=True"
    )
    assert "approval_required" not in reply, (
        "approval_required exists only as a failure_reason/stopped-reason VALUE, never a reply key"
    )
    assert_envelope(reply, result_required=False)
    assert_result_token(reply)


def assert_no_compat_flags(reply: dict[str, Any]) -> None:
    """No compatibility flags (§1 preamble): the deleted/demoted keys stay gone."""
    assert "image_scale" not in reply, "image_scale is deleted (D-3); image_outbound_scale only"
    assert "approval_required" not in reply, (
        "approval_required is a value vocabulary, never a reply key"
    )


def assert_image_scale_keys(
    reply: dict[str, Any], *, image_required: bool = True
) -> float | None:
    """§1.1 D-3: image_outbound_scale is the ONLY scale key on image-bearing replies.

    Measured applied downscale (``None`` for raw pass-through); when
    ``image_required`` the reply must actually carry the key.
    """
    assert_no_compat_flags(reply)
    if image_required:
        assert "image_outbound_scale" in reply, (
            "image-bearing reply carries image_outbound_scale (the only scale key)"
        )
    scale = reply.get("image_outbound_scale")
    if scale is not None:
        # MINOR-b (Commander decision): a float ONLY when a downscale was actually
        # applied (always < 1); 1.0 must be reported as None (raw pass-through).
        assert isinstance(scale, (int, float)) and 0.0 < scale < 1.0, (
            f"image_outbound_scale must be None (pass-through) or a downscale in (0, 1), "
            f"got {scale!r}"
        )
    return scale


def assert_ensure_app_outcome(reply: dict[str, Any]) -> None:
    """§1.1 S-12: ensure_app replies carry a typed ensure_app_outcome enum value
    (or None otherwise)."""
    outcome = reply.get("ensure_app_outcome", None)
    assert outcome is None or outcome in ENSURE_APP_OUTCOMES, (
        f"ensure_app_outcome {outcome!r} outside the Literal enum {sorted(ENSURE_APP_OUTCOMES)}"
    )


def assert_marker_reply(reply: dict[str, Any]) -> None:
    """§1.2 marker (done/wait): intentional non-screen claim, ok=True,
    verification_method='no_op_marker', confidence 1.0."""
    assert_result_token(reply, surface=None)
    assert reply["result"] == "marker", f"expected result='marker', got {reply['result']!r}"
    assert reply.get("ok") is True, "marker is ok=True (§1.1 ok-rule)"
    verification = assert_verification_block(reply, required=True)
    assert verification is not None and verification.get("verification_method") == "no_op_marker", (
        "marker verification_method is 'no_op_marker'"
    )
    assert verification is not None and verification.get("confidence") == 1.0, (
        "marker carries confidence 1.0"
    )


def assert_dry_run_reply(reply: dict[str, Any]) -> None:
    """§1.2 dry_run: validated, deliberately not dispatched; ok=False,
    dry_run flag true, verification_method='dry_run'."""
    assert reply["result"] == "dry_run", f"expected result='dry_run', got {reply['result']!r}"
    assert reply.get("ok") is False, "dry_run is ok=False (§1.1 ok-rule)"
    verification = assert_verification_block(reply, required=True)
    assert verification is not None and verification.get("verification_method") == "dry_run", (
        "dry_run verification_method is 'dry_run'"
    )


def assert_plan_step_reply(reply: dict[str, Any]) -> None:
    """§1.1/§1.3 plan step reply: result in the plan-step subset;
    failure_reason + not_executed + evidence_by_backend always present."""
    assert_envelope(reply)
    assert_result_token(reply, surface="plan_step")
    assert reply.get("ok") is (reply["result"] in OK_TRUE_RESULTS), "plan step obeys the ok-rule"
    assert "failure_reason" in reply, "plan step carries failure_reason ALWAYS (not only on failure)"
    not_executed = reply.get("not_executed")
    assert isinstance(not_executed, list), (
        f"plan step carries not_executed list, got {not_executed!r}"
    )
    assert "evidence_by_backend" in reply, "plan step carries evidence_by_backend"


def assert_terminal_plan_reply(reply: dict[str, Any]) -> None:
    """§1.3 terminal plan reply: result in {verified, unknown, failed, stopped};
    ok = (result == 'verified') — the §1.4.1 fix replacing `result != 'failed'`."""
    assert_envelope(reply)
    assert_result_token(reply, surface="terminal_plan")
    expected_ok = reply["result"] == "verified"
    assert reply.get("ok") is expected_ok, (
        f"terminal ok must be (result == 'verified'); result={reply['result']!r} "
        f"requires ok={expected_ok}"
    )
    assert "failure_reason" in reply, "terminal reply carries failure_reason ALWAYS"
    not_executed = reply.get("not_executed")
    assert isinstance(not_executed, list), "terminal reply carries not_executed list"
    assert "evidence_by_backend" in reply, "terminal reply carries evidence_by_backend"


def validate_reply(reply: dict[str, Any], surface: str) -> dict[str, Any]:
    """Compose the §1 assertions for one reply of a named surface.

    Surfaces: flat_execute, follow_up_item, plan_step, terminal_plan, wait,
    cancel_wait, wait_status, fetch_image, observe, zoom, screenshot.
    Returns the reply on success; raises AssertionError with context on any
    contract violation.
    """
    assert isinstance(reply, dict), f"reply must be a dict, got {type(reply).__name__}"
    if surface in ("plan_step",):
        assert_plan_step_reply(reply)
    elif surface == "terminal_plan":
        assert_terminal_plan_reply(reply)
    elif surface in ("flat_execute", "follow_up_item"):
        assert_envelope(reply)
        assert_ok_rule(reply)
        assert_result_token(reply, surface=surface)
        if reply["result"] in ("verified", "uncertain"):
            assert_verification_block(reply, required=True)
        if reply["result"] == "failed":
            # §1.1 scopes verification to DISPATCHED input actions; a failed reply
            # from a never-dispatched refusal (operation error) legitimately has no
            # block — when one IS present it must still be well-formed (VAL-WP1
            # delta-2 helper amendment, recorded).
            assert_verification_block(reply, required=False)
        if reply["result"] == "rejected":
            assert_rejection(reply)
        if reply["result"] == "marker":
            assert_marker_reply(reply)
        if reply["result"] == "dry_run":
            assert_dry_run_reply(reply)
    elif surface in ("wait", "cancel_wait", "wait_status", "fetch_image"):
        assert_envelope(reply)
        assert_ok_rule(reply)
        assert_result_token(reply, surface=surface)
    else:
        # read-only surfaces (observe/zoom/screenshot): no result token (§1.3)
        assert_envelope(reply, result_required=False)
        assert "result" not in reply, f"read-only surface {surface} carries no result token"
    if surface in _IMAGE_BEARING_SURFACES:
        assert_image_scale_keys(reply, image_required=surface in ("observe", "zoom", "screenshot"))
    assert_no_compat_flags(reply)
    return reply
