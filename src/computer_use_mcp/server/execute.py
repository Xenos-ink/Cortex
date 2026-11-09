"""The computer_execute tool: the flat action / follow-ups queue / waypoint-plan entry point (LAYER 4)."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from mcp.types import ImageContent, TextContent

from ..core.limits import LimitExceeded
from ..core.models import MAX_FOLLOW_UPS, ActionSpec, GroundedAction, ok_from_result
from ..core.state import TaskStopped
from ..pipeline.agent import execute_result_token, rejection_reasons
from ..planning import plan as plan_layer
from ..planning.plan import PlanLease
from ..vision.evidence import register_frame
from ..vision.visual_views import (
    InvalidVisualViewError,
    VisualViewError,
    encode_png_b64,
    observation_for_payload,
)
from .instance import mcp
from .lifecycle import _get_live_bundle, _session_scoped
from .observe import _resolve_view_param
from .plan_surface import (
    _audit_plan_event,
    _execute_plan_step,
    _handle_plan_action,
    _plan_guard,
    _plan_lease_refusal,
)
from .pointer_reply import executed_pointer_fields
from .replies import (
    IMAGE_OUTBOUND_SCALE_KEY,
    _action_image_full,
    _execute_response_blocks,
    _redact_queue_entry,
    _redact_result_payload,
)
from .schemas import (
    NullableBool,
    NullableCoordinate,
    NullableFollowUps,
    NullableKeys,
    NullablePlan,
    NullableRecord,
    NullableStr,
    _coerce_follow_up_entry,
    _coerce_tolerant_follow_ups,
    _teaching_invalid_action,
)
from .state import (
    IMAGE_DELIVERY_TEXT_NOTE,
    _session_image_delivery,
    _stopped_session_message,
    _StoppedSession,
    _UnknownSession,
)


@mcp.tool()
@_session_scoped
async def computer_execute(
    session_id: str,
    action: str,
    x: NullableCoordinate = None,
    y: NullableCoordinate = None,
    text: NullableStr = None,
    keys: NullableKeys = None,
    delta: int = 0,
    approved: bool = False,
    expected_effect: NullableStr = None,
    x2: NullableCoordinate = None,
    y2: NullableCoordinate = None,
    target: NullableStr = None,
    via: NullableStr = None,
    include_screenshot_after: NullableBool = None,
    follow_ups: NullableFollowUps = None,
    visual_view: NullableStr = None,
    plan: NullablePlan = None,
    plan_action: NullableStr = None,
    record: NullableRecord = None,
) -> Any:
    """Validate and execute one grounded action; approval applies only to this action call.

    session_id is REQUIRED on every call: copy it from start_session's result and
    reuse it for the whole session.

    ACTION VOCABULARY (exact names, from the models enum): click, double_click,
    right_click, drag, type, keypress, scroll, wait, done, move, hotkey, focus_window,
    ensure_app. There is
    NO "key" action (single keys use "keypress": {"action": "keypress", "keys": ["ctrl"]})
    and NO "triple_click" (use "double_click" or repeated clicks). "keys" must be KEY
    NAMES ("ctrl", "a", "enter", "esc") — never text or sentences; to type text use
    {"action": "type", "text": "..."}. "ensure_app" takes target="process" or
    "process|doc-token" (e.g. "excel|book1") and attaches to an EXISTING instance
    (REATTACHED / AMBIGUOUS_INSTANCE / NO_INSTANCE payloads; with the default
    launch="server" policy a NO_INSTANCE launches the process server-side — when a
    process allowlist is configured only an allowlisted target may launch, and when
    NO process allowlist is configured any resolvable target may (the pipeline's
    ``_ensure_app_allow_launch`` gate); launch="driver" or
    CORTEX_ATTACH_OR_LAUNCH=driver keeps the never-launch behavior).

    The hardcoded ``confidence=1.0`` is the client-asserted MODEL confidence; grounding,
    staleness, risk, approval, and verification run independently. ``expected_effect``
    opts into semantic verification (a stated effect must be observed or the result
    reports verification failed/uncertain — never silently successful). For
    ``action="drag"``, ``x``/``y`` are the drag start and the trailing ``x2``/``y2`` the
    drag end (both required, screenshot coordinates). ``action="move"`` requires
    ``x``/``y``; ``action="hotkey"`` requires ``keys`` (2-12 key names);
    ``action="focus_window"`` requires ``target`` (a window title).
    ``action="scroll"`` turns the wheel at the CURRENT CURSOR POSITION with
    POSITIVE = UP, away from you (negative reads further down) — ``move`` the pointer
    to the target region first; ``delta`` is bounded -20..20.

    Type transport selector (v0.7.1, type actions ONLY): ``via`` chooses HOW text is
    delivered. ``via="sendinput"`` (default; also ``via=null``) is per-character
    ``KEYEVENTF_UNICODE`` injection. ``via="clipboard"`` saves the current clipboard,
    sets ``CF_UNICODETEXT`` with the typed text, dispatches the ctrl+v chord through the
    existing hotkey path, then best-effort restores the previous clipboard content
    (on the executed path a restore failure is typed-annotated
    ``clipboard_restore=failed`` and never fails the action; on an ABORT between the
    clipboard set and the chord the prior text is restored when it was readable
    (best effort) but the annotation is lost
    with the exception — a known abort-path limitation). Use the clipboard
    transport for OpenGL/console apps that receive chords but drop per-character
    injection (field case: Blender's Python Console). GATE PARITY: the transport never
    bypasses anything — the SAME classification, redaction, and approval decision is
    computed for both transports from the identical text; they differ only after
    approval. When the target is unreadable (no accessibility tree), a blind type
    reports ``TYPE_UNCONFIRMED no-readable-target`` and recommends
    ``via="clipboard"`` plus visual verification with a stated ``expected_effect``.
    ``via`` on any non-type action, or an unknown value, is a typed ``invalid_action``
    rejection (fail closed).

    Held input duration (WP2, ORVEX-2026-107): ``hold_ms`` is an OPTIONAL integer,
    0..5000 MILLISECONDS, carried on an INDIVIDUAL ACTION — a ``follow_ups`` entry —
    and legal on ``click``,
    ``right_click``, ``keypress`` and ``hotkey`` ONLY. Every other action
    (``double_click``, ``move``, ``scroll``, ``type``, ...) is rejected fail-closed
    with the typed ``invalid_action`` whose message reads "hold_ms is only valid on
    click/right_click/keypress/hotkey actions". On a key action it presses the key,
    keeps it down for the duration, then releases it; on ``hotkey`` it holds EVERY
    named key down SIMULTANEOUSLY for the duration and releases them in REVERSE
    order — a ``shift`` a member requires is synthesized and released right after
    every key that required it, never before one, so a chord like ``["ctrl","A"]``
    releases shift before ctrl. It is NOT "chord, then wait" — that would hold
    nothing, because a chord has already released every key by the time it returns.
    ``hold_ms=0`` is an explicit zero-duration press/release through that SAME path,
    not a fast-path chord; OMITTING it (or ``null``) keeps the ordinary immediate
    dispatch unchanged — for a key action, the existing atomic chord. It is a
    duration on ONE input and never a motion path: ``duration_ms`` is a SEPARATE,
    ``move``-only field, and the two must not be conflated.

    REM-F weak-model tolerance (boundary only): integer coordinates also accept
    integral floats (1343.0) and numeric strings ("1343"); NON-INTEGRAL values ROUND
    to the nearest int (a vision model aiming at pixel 1343.7 must not hard-fail).
    The D5-widened advertised schema admits integer | string | null; non-numeric
    garbage ("left") still fails with a clean typed error. ``follow_ups`` also
    accepts a JSON-encoded string containing the list, a single dict (wrapped into a
    one-entry list), and per-entry JSON strings — same fail-closed queue semantics
    afterwards.

    Host-payload opt-out (PERF-004, trailing optional): pass
    ``include_screenshot_after=false`` to OMIT the heavy image block
    (and any image bytes) from this response — recommended for actions whose outcome
    you check via the verification verdict + digest instead of the image (the full
    image stays available via computer_observe). DEFAULT action responses carry a
    HALF-SIZE JPEG (0.5x scale, ~60KB typical at 1080p) to keep your context light;
    ask for ``include_screenshot_after=true`` when FULL resolution is required (the
    full image stays available via computer_observe/computer_screenshot either way),
    or set ``CORTEX_ACTION_IMAGE_FULL=1`` to restore full-res defaults. Result image
    budget (REM-A H7): every outbound image is kept under ``CORTEX_RESULT_IMAGE_MAX_KB``
    (default 180 KB; oversized PNG re-encodes as JPEG outbound only — internal
    captures stay PNG). Executed responses return MCP content blocks (TextContent
    result JSON + ImageContent post-action screenshot, parity with computer_observe);
    error/rejection/approval shapes stay plain dicts. In a text-mode session
    (``start_session(image_delivery="text")``) the executed response is the slim dict
    (no image block, no screenshot bytes, mode keys added) and
    ``include_screenshot_after`` cannot re-enable images in text mode.

    Queued actions (PERF-004, trailing optional): ``follow_ups`` is a list of at most 5
    action specs (same fields as this tool's action parameters, e.g.
    {"action": "click", "x": 10, "y": 20, "expected_effect": "..."}), PLUS the two
    duration fields that cannot be passed as tool parameters: ``hold_ms`` (held
    input, click/right_click/keypress/hotkey only) and ``duration_ms`` (timed
    ``move`` motion) — so a queued item behaves EXACTLY like the equivalent flat
    action. Each follow-up
    passes the FULL independent pipeline (validate -> safety -> approval semantics ->
    execute -> verify) exactly like a single action — zero bypass. The queue CONTINUES
    while the attached window stays the same (ordinary pixel changes from
    earlier items — drawing, typing, dialogs — do NOT stop it; each item re-grounds
    from the fresh post-action capture). It stops only on TRUE staleness — the
    attached window's identity (hwnd/title/bounds) changed or closed since the queued
    premise — plus safety rejection, approval requirement, validator/grounding
    rejection, dispatch error, or a named interference event. Practical pattern:
    commit your in-progress UI state BEFORE switching ribbon tools — e.g. while a
    Paint shape is in edit mode the ribbon swallows clicks (exactly 0 pixel change),
    so click once on the canvas to commit the shape, then use the next ribbon tool.
    ``CORTEX_QUEUE_STRICT_DIGEST=1`` restores the legacy whole-screen stop.
    An UNCERTAIN verification does NOT stop the queue, and neither does a "failed"
    verification of an EXECUTED item — the keystroke/click physically dispatched, so
    the honest failed verdict (ok=false) rides that item's entry in follow_up_results
    while the rest of the batch still runs. Batch small related groups and end the
    batch with an observation.
    Per-item results arrive in the additive ``follow_up_results`` field (bounded, no
    per-item screenshots) with ``follow_ups_stopped_reason`` (None = the batch ran to
    completion).
    An item that is ActionSpec-valid but GroundedAction-invalid (a target-less
    ``focus_window``, a half-specified ``drag`` with one endpoint missing, a 1-key
    ``hotkey``, a point-less ``move``, a target-less ``ensure_app``) is rejected at
    the boundary with the typed ``invalid_action`` error and teach-in-text hints
    listing the valid shapes — nothing dispatches and the queue never starts (R-18;
    never a raw pydantic ValidationError).

    Visual view (AL-002, trailing optional): ``visual_view`` selects the representation
    of the RETURNED post-action image — "raw" (untouched stock bytes + additive
    ``visual_view``/``visual_view_scale`` keys) or "grid" (a derived coordinate-grid
    annotation; derived ONCE per response, then the UNCHANGED stock half-res/ladder
    bounding). Absent request = byte-identical default behavior; unknown values are
    rejected fail-closed (``invalid_visual_view``) BEFORE the action runs — a bad view
    name never dispatches input. If the view cannot be derived from the post-action
    payload, the executed result is still returned honestly (an executed action's
    outcome is never hidden) but with NO image block and an additive
    ``visual_view_error: "view_derive_failed: ..."`` marker — never a silent raw
    substitution. In text mode (no image is served either way) the keys do not apply.

    Waypoint plan (ORVEX-2026-101 Batch 2, trailing optional ``plan``): a plan
    declares a sequence of POINTS + ACTIONS and Cortex executes it STEPWISE — you
    declare LOCATIONS AND ACTIONS ONLY; the mouse motion BETWEEN points is an
    internal detail (deterministic defaults; optionally overridable per point with
    ``duration_ms``; there are NO path/bend/easing parameters). Shape:
    {"points": [{"id": "open-menu", "x": 420, "y": 180, "actions": [{"action": "click"}],
                 "arrival_condition": "menu is open"},
                {"id": "save", "x": 850, "y": 720, "actions": [{"action": "click"}]}]}.
    Every point normalizes internally to a ``move`` transition + its actions, and
    EVERY item rides the FULL independent pipeline (ground -> validate -> safety ->
    approval -> execute -> verify) exactly like a single action — the "wait and
    re-observe" discipline is inherited per item, never bypassed. Point actions
    carry NO x/y (the point IS the location); x2/y2 belong to a queued ``drag``
    only. ``arrival_condition`` rides as ``expected_effect`` for the point's
    actions that lack an explicit one. A plan and the flat action/follow_ups
    surface are mutually exclusive (pass EITHER; a plan call leaves every flat
    action parameter at its default). An invalid or INCOMPLETE plan is a typed
    ``invalid_action`` rejection BEFORE any capture or motion. At most 10 points
    and 5 actions per point.

    MOTION AND TIMING — a plan is the EXPLICIT-TIMING mode (ORVEX-2026-102 §5.2).
    A bare action is the INSTANT mode: outside a plan the move duration and the
    settle are both 0 ms, so a single action is dispatched as fast as the machine
    allows and NOTHING is hidden. A SUBMITTED plan must therefore STATE all of the
    following, or it is rejected BEFORE any capture or motion, naming what is
    missing: ``points`` in order and ``actions`` at each point (an explicit empty
    ``"actions": []`` declares a crossing point); the movement timing
    (``transition_duration_ms`` on the plan, or ``duration_ms`` on every point);
    an explicit ``delta`` on every ``wait`` action inside a point (0..10 s;
    ``final_wait_ms`` never covers it); the settle wanted (``settle_ms``, 0 = no
    settle, 1..200); what is verified after each step (``arrival_condition``, or an
    action-level ``expected_effect``); the per-step cooperative timeout
    (``point_timeout_ms``, or ``timeout_ms`` per point); and BOTH ``on_failure``
    and ``on_uncertain`` ("stop"/"continue"). Optional plan-level keys:
    ``final_wait_ms`` (one trailing wait, capped 5000 ms) and inherited
    ``settle_ms``/policies/timeout/duration.

    Stepwise handshake (ORVEX-2026-101 decision 1, trailing optional
    ``plan_action``): the FIRST plan call executes ONE waypoint step, then returns
    ``blocked=true`` with an explicit JSON result/evidence/prefix/next_step/
    remaining block, and a plan LEASE stays active on the session. While the lease
    is active, flat actions, follow_ups, observation tools
    (computer_observe/zoom/screenshot), computer_wait and computer_cancel_wait are
    refused with the typed ``plan_lease`` error before any capture or input —
    computer_wait_status and computer_fetch_image stay allowed (read-only). A
    misdirected call costs nothing. ``plan_action="continue"`` runs the NEXT step
    under the SAME approval verdict (approval is decided once, on the FIRST plan
    call, and inherited by every step and by ``replace`` — ``continue`` never
    re-asks); ``plan_action="stop"`` releases the lease and returns the executed
    prefix with ``result:"stopped"``; ``plan_action="replace"`` validates a
    complete replacement plan (required), closes the old lease, and opens a new
    one inheriting the FIRST call's approval. A plan whose steps abstain reports
    ``result:"uncertain"`` per step and TERMINATES with ``result:"unknown"`` —
    ``ok`` is true ONLY when EVERY step verified (``ok = (result ==
    "verified")``); ``unknown`` and ``stopped`` terminal verdicts are never
    success.

    Plan replies (unified contract §1.3): every step and terminal reply carries
    ``result`` (step: verified/uncertain/failed/rejected/not_executed; terminal:
    verified/unknown/failed/stopped), ``failure_reason`` (ALWAYS; ``null`` when
    nothing failed; ONE token of the closed vocabulary ``plan.PLAN_FAILURE_REASONS``
    — the full B6-B8 classification tables), ``not_executed`` (the
    bounded, explicit list of plan work that never ran, each entry carrying the
    reason), and ``evidence_by_backend`` (a DICT: ``captures`` from the session's
    evidence store — real on BOTH backends — plus ``repositions`` the plan's moves
    recorded). The bare ``evidence`` key stays a LIST of evidence lines everywhere
    else; the two are never mixed. The aggregate ``plan`` block reports per-point
    status and per-action rows (including each action's ``hold_ms``).

    The lease is released exactly once — on the terminal reply, a host
    ``stop``/``replace``, a structural step error or exception, or the session
    ending (stop_session, the idle timeout, an internal kill). An idle lease with
    no call in flight does NOT keep the session alive: after the idle timeout the
    session is reaped and the lease released with it.

    Record around the action (WP-E, ORVEX-2026-109, trailing optional
    ``record``): a background screen recording started JUST BEFORE this call's
    first input is dispatched — the F1 field fix (a recording started in a
    SEPARATE call always ended 15-30 s before the model's next tool call, so
    every recording finished before anything happened). Shape:
    {"duration_s": 4, "region": null, "fps": null, "max_keyframes": null,
    "include_video": null, "save": null, "video_delivery": null} — the SAME
    bands and validation as computer_record (duration 0.5..10 s; fps 1..10,
    default 5; max_keyframes 2..12, default 8; region [l,t,w,h] canonical or
    null = the session monitor; video_delivery "file" | "inline"), through the
    SAME code path with the SAME typed rejections. An INVALID record argument
    (out of band, malformed region, an UNKNOWN KEY) is a typed rejection of
    the whole call BEFORE any input — consistent with unknown-argument
    rejection. ONE coercion rule for BOTH tools (DELTA 2, the tolerant
    decision): fps/max_keyframes go through exactly the same
    `_coerce_tolerant_int` coercion as computer_record's boundary —
    non-integral floats and numeric strings ROUND the same way (2.7 → 3,
    "5.0" → 5, "1e1" → 10); duration_s accepts numeric strings like "4" (the
    lax-float shapes pydantic accepts for computer_record); booleans, lists,
    dicts and non-numeric strings give the typed
    invalid_record_fps / invalid_record_keyframes / invalid_record_duration.
    Band checks run AFTER coercion, for both tools, with the same codes. A
    short pre-roll
    (``RECORD_PRE_ROLL_SECONDS``, 0.3 s, part of the window — never added on
    top of ``duration_s``) captures the "before" state. computer_execute
    returns as it does today plus an additive ``record`` block {started,
    record_id, status:"recording", ends_at, duration_s, fps, region,
    message}; collect with computer_record_result(record_id) — it blocks
    until the recording ends and its change timeline CONTAINS this call's own
    actions. Works with flat actions, follow_ups batches and plan steps
    (accepted on the plan submission call and on plan_action="continue"
    calls; the recording starts just before the step's input;
    plan_action="stop" and a continue past the walk's end answer
    {"started": false, "reason": "no_action"}). Caps/lease/idle/audit/
    stop_session behave exactly as for computer_record (same registry): if a
    cap is reached the action STILL runs and the block says {"started": false,
    "reason": "recording_limit"} — a recording problem NEVER refuses the
    action (the closed started:false reason set: recording_limit /
    text_session_no_screen / invalid_region / session_stopped /
    record_start_failed / no_action). If the call dies after the recording
    started (CancelledError, any exception) the recording is CANCELLED before
    the error propagates, so the slot frees; if the call is refused BEFORE any
    input (safety denial, approval hold, a zero-effect rejection) the
    recording is cancelled too and the block gains ``cancelled:
    "action_refused"``.

    Batch replies (WP-E E3): when a call carries follow_ups, the reply's
    attached image — and its image_id, pointer and last_click, and the frame
    registered in the evidence store — is the post-action frame of the LAST
    item that executed (a trailing wait counts: click + click + wait 8 shows
    the board after the opponent replied). If the batch stopped early it is
    the last item that actually executed. The verdict fields
    (result/verification/ok) keep their item-0 meaning; the additive
    ``image_after_item`` key names the item whose frame is shown — ALWAYS
    present on a multi-item batch's image-bearing reply (including the value
    0, when item 0 is the last executed one), absent on single-action calls
    (byte-identical to before) and on replies that serve no image. Pressed/
    Normal follows the same rule applied to the shown item: Pressed if it was
    a click type and the frame is its own post-click frame.

    Example: {"session_id": "S", "action": "click", "x": 640, "y": 360, "expected_effect": "the OK dialog closes"}
    Example (act, wait, see — ONE call; the reply image shows the state after the LAST item, "image_after_item" names it): {"session_id": "S", "action": "click", "x": 500, "y": 400, "expected_effect": "my pawn is selected", "follow_ups": [{"action": "click", "x": 500, "y": 320}, {"action": "wait", "delta": 8}]}
    Example (record around the action): {"session_id": "S", "action": "click", "x": 980, "y": 44, "record": {"duration_s": 4}}
    """
    try:
        bundle = _get_live_bundle(session_id)
    except (_StoppedSession, _UnknownSession) as exc:
        # §1.1/§1.4 (ORVEX-2026-108), execute surface ONLY: a session-lifecycle
        # refusal of an ACTION call is a rejection — {ok False, result "rejected",
        # message, reasons}, the typed code named as the first reason, no ``error``
        # key. The read-only/lifecycle tools keep the typed ``_error_response``
        # shapes (§1.3).
        code = "session_stopped" if isinstance(exc, _StoppedSession) else "unknown_session"
        return {
            "ok": False,
            "result": "rejected",
            "message": (
                _stopped_session_message(str(exc.args[0]) if exc.args else "")
                if isinstance(exc, _StoppedSession)
                else str(exc)
            ),
            "reasons": [code],
            "session_id": str(exc.args[0]) if exc.args else "",
        }
    # WP-E E1: the record helpers are imported HERE (function-local, the
    # plan_surface precedent): a module-level import would make execute.py
    # import record.py, and record's tools would register BEFORE execute's —
    # breaking the pinned monolith registration order (test_server_import_order
    # A2: ... zoom -> execute -> fetch -> record).
    from .record import (
        EXECUTE_RECORD_ALLOWED_KEYS,
        RECORD_PRE_ROLL_SECONDS,
        _resolve_recording_space,
        _text_session_record_block,
        _validate_record_arguments,
        cancel_execute_recording,
        execute_record_input_dispatched,
        start_execute_recording,
    )
    # D1: text mode dominates every executed response shape — include_screenshot_after
    # can only REMOVE bytes, never re-enable an image (a non-vision model passing
    # True must not be able to kill its own session).
    image_mode = _session_image_delivery(bundle)
    # WP-E E1: validate the ``record`` object FIRST (fail-closed, zero-effect) —
    # the SAME bands/validation as computer_record through the SAME code path,
    # with the SAME typed rejection codes; a region also pre-resolves through
    # ``_resolve_recording_space`` so an out-of-bounds/UNVERIFIABLE region is
    # rejected BEFORE any input (consistent with unknown-argument rejection).
    # The launch itself happens later, just before the first dispatch.
    record_to_launch: Any | None = None
    record_text_blocked = False
    if record is not None:
        if not isinstance(record, dict):
            return {
                "ok": False,
                "result": "rejected",
                "message": "record must be an object of recording parameters.",
                "reasons": ["invalid_record_param"],
            }
        # DELTA 1 n4/m3: an unknown key inside ``record`` is a typed rejection
        # (consistent with the unknown-argument rule), naming the allowed keys —
        # never a silent ignore.
        unknown_keys = sorted(set(record) - set(EXECUTE_RECORD_ALLOWED_KEYS))
        if unknown_keys:
            return {
                "ok": False,
                "result": "rejected",
                "message": (
                    "Unknown key(s) in the record object: "
                    + ", ".join(unknown_keys)
                    + ". Allowed keys: "
                    + ", ".join(EXECUTE_RECORD_ALLOWED_KEYS)
                    + "."
                ),
                "reasons": ["invalid_record_param"],
            }
        # DELTA 2 N1/N2: the validator COERCES (the shared TolerantInt /
        # lax-float rule computer_record goes through) and RETURNS the
        # resolved values — the launch consumes those, never raw input.
        code, message, resolved_args = _validate_record_arguments(
            record.get("duration_s"),
            record.get("fps"),
            record.get("max_keyframes"),
            record.get("include_video"),
            record.get("save"),
            record.get("video_delivery"),
            record.get("region"),
        )
        if code is not None or resolved_args is None:
            return {
                "ok": False,
                "result": "rejected",
                "message": message or "invalid record arguments.",
                "reasons": [code or "invalid_record_param"],
            }
        resolved_space = _resolve_recording_space(bundle, record.get("region"))
        if isinstance(resolved_space, dict):
            return {
                "ok": False,
                "result": "rejected",
                "message": str(resolved_space.get("message", "invalid recording region.")),
                "reasons": [str(resolved_space.get("error", "invalid_region"))],
            }
        # A text-mode session has no screen images: the recording reports
        # {"started": false} in the reply — the ACTION still runs (never a
        # refusal).
        if image_mode == "text":
            record_text_blocked = True
        else:
            record_to_launch = resolved_args
    # ORVEX-2026-101 decision 1 (WP7b): the lease gate is the FIRST INPUT-RELEVANT
    # operation after session lookup — DELTA 1 n5: the ``record`` band/region
    # validation above runs BEFORE it (a malformed parameter is a zero-effect
    # rejection with zero input, the same fail-closed class as a bad
    # visual_view). A flat/follow-up call, a second plan, or a plan payload
    # without an explicit continuation decision is refused before visual-view
    # resolution, validation, capture, or backend dispatch: zero-effect handshake
    # rejection. A plan lease is released only by continue/stop/replace, the
    # terminal paths inside the step machinery, or session teardown — never leaked.
    active_lease = _plan_guard.active_lease(bundle)
    if active_lease is not None:
        if not (
            action == "done"
            and x is None
            and y is None
            and text is None
            and not keys
            and delta == 0
            and expected_effect is None
            and x2 is None
            and y2 is None
            and target is None
            and via is None
            and follow_ups is None
        ):
            return _plan_lease_refusal(
                bundle,
                active_lease,
                "A stepwise plan lease is active on this session; flat actions and follow-ups are refused with zero effect.",
            )
        if plan is not None and plan_action != "replace":
            return _plan_lease_refusal(
                bundle,
                active_lease,
                "A replacement plan requires plan_action='replace'; the active lease was not touched.",
            )
        if plan is None and plan_action is None:
            # DELTA 1 #13: a bare call (defaults, no plan_action) mid-lease is a
            # plan_lease refusal naming the handshake verb it must send — NOT the
            # invalid_plan_action reply, which is reserved for a GIVEN-but-invalid
            # plan_action.
            return _plan_lease_refusal(
                bundle,
                active_lease,
                "A stepwise plan lease is active on this session; send plan_action='continue', 'stop' or 'replace' (with a replacement plan only for 'replace').",
            )
        return await _handle_plan_action(
            bundle,
            active_lease,
            plan_action=plan_action,
            plan=plan,
            include_screenshot_after=include_screenshot_after,
            image_mode=image_mode,
            view_provider=None,
            record=record_to_launch,
            record_text_blocked=record_text_blocked,
        )
    if plan_action is not None:
        # WP13 (field e5): teach the driver HOW to start — the pre-fix message did
        # not say that starting means sending plan=... WITHOUT plan_action.
        return {
            "ok": False,
            "result": "rejected",
            "reasons": ["invalid_plan_action"],
            "error": "invalid_plan_action",
            "message": (
                "To START a plan, send plan=... WITHOUT plan_action. While a "
                "stepwise plan lease is active, the only valid plan_action values "
                "are 'continue', 'stop' and 'replace'."
            ),
        }
    # AL-002: resolve an explicit view request BEFORE anything runs (fail-closed — a
    # bad view name never dispatches input); the absent request adds only this
    # None-check (H19).
    try:
        view_provider = _resolve_view_param(visual_view)
    except InvalidVisualViewError as exc:
        # §1.1/§1.4, execute surface ONLY: the malformed-parameter refusal of an
        # ACTION call is a rejection (the typed code names itself as the first
        # reason); observe/zoom keep the typed ``invalid_visual_view`` error (§1.3
        # read-only refusals).
        return {
            "ok": False,
            "result": "rejected",
            "message": str(exc),
            "reasons": [f"invalid_visual_view: {exc}"],
        }
    # ORVEX-2026-101 decision 1 (WP7b): a new plan is fully validated, normalized,
    # and pre-converted BEFORE the lease is installed — a malformed or incomplete
    # plan is a typed teaching rejection with ZERO effect (no capture, no motion,
    # no lease). Only then does the first point step execute. There is NO legacy
    # single-call plan path (S-01): a plan is always stepwise.
    if plan is not None:
        if (
            action != "done"
            or x is not None
            or y is not None
            or text is not None
            or keys
            or delta != 0
            or expected_effect is not None
            or x2 is not None
            or y2 is not None
            or target is not None
            or via is not None
            or follow_ups is not None
        ):
            return _teaching_invalid_action(
                ValueError(
                    "pass EITHER a plan (waypoints+actions) OR a flat action/follow_ups "
                    "call — not both; a plan call leaves every flat action parameter at "
                    "its default (action=\"done\", no x/y/text/keys/...)."
                ),
                action,
            )
        try:
            plan_model = plan_layer.coerce_tolerant_plan(plan)
            plan_specs, plan_context = plan_layer.normalize_plan(plan_model)
            for plan_spec in plan_specs:
                # R-18: every plan item must CONVERT to a GroundedAction before the
                # first motion — the plan never starts on an unconvertible item.
                plan_spec.to_grounded(reason_prefix="MCP plan action")
        except Exception as exc:  # noqa: BLE001 - malformed plan: fail closed, teach
            return _teaching_invalid_action(exc, action)
        lease = PlanLease(
            model=plan_model,
            specs=plan_specs,
            context=plan_context,
            steps=plan_layer.plan_step_slices(plan_specs, plan_context),
            # O-03: require_approval is evaluated ONCE, on this FIRST plan call,
            # for the whole plan — every step inherits this verdict and ``replace``
            # inherits it too; ``continue`` never re-asks.
            approved=bool(approved),
        )
        try:
            _plan_guard.install(bundle, lease)
        except ValueError as exc:
            # F15 (VAL-WP8-01): a plan-lease refusal is a REJECTED reply like the
            # other plan_lease refusals — the closed result token, ok False, and
            # the typed code as the first reason (assert_plan_refusal).
            return {
                "ok": False,
                "result": "rejected",
                "reasons": ["plan_lease"],
                "error": "plan_lease",
                "blocked": True,
                "message": str(exc),
            }
        # WP8: the plan lifecycle is audited from the FIRST call (E-05: the reply
        # this call returns is the step reply built below; the start event records
        # the declaration, the step reply's own plan_point records the verdict).
        _audit_plan_event(
            bundle,
            "plan_start",
            lease=lease,
            result="started",
            point_count=plan_context["point_count"],
            action_types=[spec.action.value for spec in plan_specs],
            on_failure=plan_model.on_failure,
            on_uncertain=plan_model.on_uncertain,
            transition_duration_ms=plan_specs[0].duration_ms,
            final_wait_ms=plan_context["final_wait_ms"],
        )
        return await _execute_plan_step(
            bundle,
            lease,
            include_screenshot_after=include_screenshot_after,
            image_mode=image_mode,
            view_provider=view_provider,
            record=record_to_launch,
            record_text_blocked=record_text_blocked,
        )
    if follow_ups is not None:
        # REM-F: normalize the tolerant shapes for DIRECT callers too (the boundary
        # coercion already produced list[dict] for MCP callers — idempotent here:
        # a list[dict] passes through unchanged; JSON strings parse; a dict wraps).
        try:
            follow_ups = _coerce_tolerant_follow_ups(follow_ups)
        except ValueError as exc:
            return _teaching_invalid_action(exc, action)
        assert isinstance(follow_ups, list)
        if any(not isinstance(item, dict) for item in follow_ups):
            return _teaching_invalid_action(
                ValueError("follow_ups must be a list of action-spec objects."), action
            )
        if len(follow_ups) > MAX_FOLLOW_UPS:
            return _teaching_invalid_action(
                ValueError(
                    f"follow_ups supports at most {MAX_FOLLOW_UPS} entries "
                    f"(got {len(follow_ups)}); batch smaller groups."
                ),
                action,
            )
    try:
        grounded = GroundedAction(
            action=action,  # type: ignore[arg-type]
            point=None if x is None or y is None else {"x": x, "y": y},
            to_point=None if x2 is None or y2 is None else {"x": x2, "y": y2},
            text=text,
            keys=keys or [],
            delta=delta,
            reason="Explicit MCP action",
            confidence=1.0,
            expected_effect=expected_effect,
            target=target,
            via=via,
        )
    except Exception as exc:  # noqa: BLE001 - structured error, no traceback
        return _teaching_invalid_action(exc, action)
    specs: list[ActionSpec] = []
    if follow_ups:
        for item in follow_ups:
            try:
                # REM-F: tolerate the same weak-model coordinate shapes inside
                # entries; every strict ActionSpec bound still applies afterwards.
                spec = ActionSpec.model_validate(_coerce_follow_up_entry(item))
            except Exception as exc:  # noqa: BLE001 - malformed queue item: fail closed
                return _teaching_invalid_action(exc, str(item.get("action", "")))
            try:
                # R-18: an item can be ActionSpec-valid yet GroundedAction-invalid
                # (target-less focus_window, half-specified drag, 1-key hotkey).
                # Validate the conversion HERE so the host gets the typed
                # ``invalid_action`` rejection with teach-in-text hints instead of an
                # uncaught pydantic ValidationError from inside the queue; the queue
                # never starts (fail-closed, zero items dispatch — unchanged).
                spec.to_grounded()
            except Exception as exc:  # noqa: BLE001 - grounded-shape failure: teach
                return _teaching_invalid_action(exc, spec.action.value)
            specs.append(spec)
    # WP-E E1: start the recording JUST BEFORE the first input of this call is
    # dispatched — a launch problem (cap reached, ...) NEVER refuses the
    # action; the reply's ``record`` block reports it honestly.  The pre-roll
    # (a bounded sleep, part of the recording window) lets the recorder
    # capture the "before" state first.  DELTA 1 m4: the pre-roll AND the
    # whole action are covered — a call that dies after the recording started
    # (asyncio.CancelledError, a stop, any exception) CANCELS the recording
    # before propagating (the cancel flag + the loop wake, exactly like
    # cancel=true), so the session's recording slot frees immediately instead
    # of holding for up to ``duration_s``.
    record_block: dict[str, Any] | None = None
    if record_text_blocked:
        record_block = _text_session_record_block()
    elif record_to_launch is not None:
        record_block = start_execute_recording(bundle, session_id, record_to_launch)
    try:
        if record_block is not None and record_block.get("started"):
            await asyncio.sleep(RECORD_PRE_ROLL_SECONDS)
        outcome = await bundle.agent.run_single(
            bundle.state,
            grounded,
            approved=approved,
            expected_effect=expected_effect,
            follow_ups=specs or None,
        )
    except TaskStopped as exc:
        # §1.2: a host-requested stop is the ``stopped`` token — nothing verified,
        # ok=False by the shared ok-rule.
        stopped_token = "stopped"
        stopped_response: dict[str, object] = {
            "ok": ok_from_result(stopped_token),
            "result": stopped_token,
            "stopped": True,
            "message": str(exc),
        }
        # WP-E E1: the recording may already run around this call — the agent
        # gets its id even on the stopped envelope.
        if record_block is not None:
            stopped_response["record"] = record_block
        return stopped_response
    except LimitExceeded as exc:
        # §1.1/§1.4: a limit trip is a GATE REFUSAL — verified pre-dispatch (the
        # observation-phase rate ceiling and ``check_action`` both fire BEFORE the
        # triggering action dispatches any input), so this is a rejection: the typed
        # limit named as the first reason, the additive ``limit`` key kept, no
        # ``error`` key.
        limit_response: dict[str, object] = {
            "ok": False,
            "result": "rejected",
            "message": str(exc),
            "reasons": [f"limit_exceeded: {exc.limit_name}"],
            "limit": exc.limit_name,
        }
        # WP-E E1: the recording may already run around this call.
        if record_block is not None:
            limit_response["record"] = record_block
        return limit_response
    except BaseException:
        # DELTA 1 m4: nothing below will run for this call — cancel the
        # recording it started (best-effort) so the slot frees, then propagate
        # unchanged (asyncio.CancelledError included).
        cancel_execute_recording(session_id, record_block)
        raise
    # §1 (ORVEX-2026-108 unified reply contract): every reply carries a ``result``
    # token from the closed §1.2 set, and ``ok`` is derived from that token through
    # the ONE shared ok-rule (ok is True iff result ∈ {verified, marker}) — never a
    # per-shape rule.
    if outcome.kind == "rejected":
        response: dict[str, object] = {
            "ok": False,
            "result": "rejected",
            # T8: rejections carry their SPECIFIC message (e.g. "Focus interference: the
            # OS-focused window is not the session target.") instead of the generic
            # grounding text; the structured event payloads ride in ``reasons``.
            "message": outcome.message or "Action rejected (no gate detail available).",
            "reasons": outcome.reasons,
        }
        if outcome.partial_delivery is not None:
            # §1.1 K-05: mid-type focus drift — the reply names what already landed.
            response["partial_delivery"] = dict(outcome.partial_delivery)
    elif outcome.kind == "safety_denied":
        # §1.2: the safety gate refused — a rejection; the reasons come from the SAME
        # shared helper as every queue entry (typed code names itself first).
        response = {
            "ok": False,
            "result": "rejected",
            "message": outcome.message,
            "reasons": rejection_reasons(outcome),
        }
    elif outcome.kind == "approval_required":
        # §1.1 approval rule: ok=False, result="not_executed", requires_approval=True.
        response = {
            "ok": False,
            "result": "not_executed",
            "requires_approval": True,
            "message": outcome.message,
        }
    elif outcome.kind == "digest_surprise":
        # §1.2: the premise-validity gate refused before dispatch — a rejection; the
        # reasons come from the SAME shared helper as every queue entry (the ``error``
        # key is gone: rejected replies carry reasons, never an error key).
        response = {
            "ok": False,
            "result": "rejected",
            "message": outcome.message,
            "reasons": rejection_reasons(outcome),
        }
    elif outcome.kind == "error":
        # §1.2: an operation error is ``failed`` (ok=False); the typed code stays.
        response = {
            "ok": False,
            "result": "failed",
            "error": "action_error",
            "message": outcome.message,
        }
    else:
        result = outcome.result
        if result is None:  # defensive: executed outcomes always carry a result
            # DELTA 1 m4: the defensive envelope carries the record block too —
            # the agent must never lose the record_id.
            no_result_response: dict[str, object] = {
                "ok": False,
                "result": "failed",
                "error": "action_error",
                "message": "Execution produced no result.",
            }
            if record_block is not None:
                no_result_response["record"] = record_block
            return no_result_response
        payload = result.model_dump()
        # F2: the response path is redacted too (defense in depth).
        payload = _redact_result_payload(payload)
        payload["result"] = execute_result_token(outcome)
        payload["ok"] = ok_from_result(payload["result"])
        payload.update(
            {
                "model_confidence": outcome.model_confidence,
                "grounding_confidence": outcome.grounding_confidence,
                "verification_confidence": outcome.verification_confidence,
            }
        )
        response = payload
    # v07-007 R-21 (Commander decision D1): the non-executed response shapes embed
    # model-proposed text in ``message`` (e.g. approval messages carry _clip(action.text)),
    # so they get the SAME redaction the executed shape already applies — additive
    # redaction only, no shape/field changes.
    if outcome.kind in {"rejected", "safety_denied", "approval_required", "digest_surprise", "error"}:
        response = _redact_result_payload(response)
    # DELTA 1 n6 + DELTA 2 n6: when the call was refused BEFORE any input was
    # dispatched, the recording it started would otherwise hold the session's
    # recording slot for up to ``duration_s`` for a screen nothing changed —
    # cancel it immediately (the cancel flag + the loop wake, exactly like
    # cancel=true) and MARK the block so the agent knows the timeline will not
    # materialize.  The zero-input test is the EXACT dispatch-ring check (DELTA
    # 2): no action stamped in the session's ring between the recording's
    # start and now means nothing was dispatched — the cancel can NEVER fire
    # after input left the process (the unsupported-key-after-dispatch
    # envelope included; the ring is stamped at the ONE dispatch choke point
    # before ``_backend_execute``).
    if (
        record_block is not None
        and record_block.get("started")
        and outcome.kind in {"safety_denied", "approval_required", "digest_surprise", "rejected"}
        and outcome.partial_delivery is None
        and not execute_record_input_dispatched(session_id, record_block)
    ):
        if cancel_execute_recording(session_id, record_block):
            record_block["cancelled"] = "action_refused"
    # WP-E E1: the reply's additive ``record`` block rides EVERY response shape
    # (started marker facts or {"started": false, reason}) — the agent collects
    # the timeline with computer_record_result(record_id), which blocks until
    # the end and whose actions/timeline contain THIS call's items.
    if record_block is not None:
        response["record"] = record_block
    # PERF-004 C4: host-payload opt-out (additive, off by default) — omit the heavy
    # image from the response entirely when the caller asked for it.
    if include_screenshot_after is False:
        # MINOR-c (§1.1 one key, one presence rule): an opt-out executed reply serves
        # no image, so the honest scale report is None (same as text mode).
        if response.pop("screenshot_after_base64", None) is not None:
            response[IMAGE_OUTBOUND_SCALE_KEY] = None  # also covers any nested path (H5)
    # PERF-004 C7: additive queue bookkeeping on every response shape.
    if outcome.follow_up_results is not None:
        response["follow_up_results"] = [
            _redact_queue_entry(entry) for entry in outcome.follow_up_results
        ]
        response["follow_ups_stopped_reason"] = outcome.follow_ups_stopped_reason
    # T8: additive Interference Guard events on every response shape (structured event
    # payloads the driver parses per benchmarks/tasks_hard/DRIVER-PROTOCOL.md).
    if outcome.interference_events:
        response["interference_events"] = list(outcome.interference_events)
    # REM-A H3 (master-mission Phase 2): EXECUTED responses return MCP content blocks
    # in parity with computer_observe — a slim TextContent (result JSON without the
    # image blob; queue entries never carry image bytes) plus one ImageContent with
    # the post-action screenshot under the CORTEX_RESULT_IMAGE_MAX_KB budget (H7).
    # Error/rejection/approval shapes have no image key and stay plain dicts.
    # include_screenshot_after=False was applied above, so the opt-out never enters
    # this branch (H5: no ImageContent, and none nested in follow_up_results).
    # D1 text mode: the slim dict shape (the opt-out precedent, mode keys added) —
    # include_screenshot_after can never promote text mode back to image mode.
    if outcome.kind == "executed" and "screenshot_after_base64" in response:
        if image_mode == "text":
            response.pop("screenshot_after_base64", None)
            response["image_delivery"] = "text"
            # MINOR-c: no image served, no scale applied — honest None.
            response[IMAGE_OUTBOUND_SCALE_KEY] = None
            response["image_delivery_note"] = IMAGE_DELIVERY_TEXT_NOTE
            return response
        # R-5 (W4): the capture-time frame rides along (never serialized; absent for
        # legacy results) so the outbound JPEG ladder skips re-decoding the PNG.
        # an EXPLICIT include_screenshot_after=true is the documented
        # full-image opt-in and ships FULL resolution; the DEFAULT (omitted) ships
        # the half-res JPEG; CORTEX_ACTION_IMAGE_FULL=1 restores full-res defaults.
        frame = getattr(result, "_frame", None) if result is not None else None
        full_resolution = include_screenshot_after is True or _action_image_full()
        # WP-C3 (ORVEX-2026-109; DELTA 1 M8): the pointer on THIS reply's
        # outbound image — the whole decision (Pressed from the shown item's
        # click point, Normal at the capture-time cursor, honest in_image
        # placeholder fixed by ``_execute_response_blocks``) lives in
        # ``executed_pointer_fields``; ``last_click`` still comes from the ring
        # and MAY name a later follow-up click — reported separately with its
        # own coordinates, which is acceptable.  WP-E E3: the shown item IS the
        # last executed one (its frame rides the reply), so its Pressed/Normal
        # rule applies — a trailing wait shows Normal.
        shown_action = (
            outcome.image_after_action
            if outcome.image_after_action is not None
            else grounded
        )
        pointer_xy, pointer_state, pointer, pointer_fields = executed_pointer_fields(
            bundle, result, shown_action
        )
        # AL-002 (additive): honor an explicit visual_view on the RETURNED image —
        # derived ONCE per response, then the UNCHANGED stock bounding branch. Raw is
        # the pass-through (stock bytes + additive keys). If the view cannot be
        # derived, the executed result is still returned honestly (an executed
        # action's outcome is never hidden behind an error dict) with NO image block
        # and the typed ``view_derive_failed`` marker — never a silent raw substitute.
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
        # WP5: THIS reply is about to carry the post-action image, so the frame is
        # retained HERE — after every early return that ends in a reply WITHOUT the
        # image (rejection, approval, opt-out pop, text mode, view-derive failure) —
        # and the reply gains the additive image_id keys.  The bytes retained are
        # EXACTLY the bytes the reply carries (VAL-WP5 F1): the DERIVED view payload
        # when an explicit non-raw view was served, otherwise the raw post-action
        # capture — both pre-ladder, so a later fetch re-runs the ladder honestly.
        # ``register_frame`` reads the store key and never creates it, so a teardown
        # that already ran writes nothing instead of resurrecting a store.
        carried_b64 = (
            str(view_payload[0]) if view_payload is not None else str(response["screenshot_after_base64"])
        )
        response.update(
            register_frame(
                bundle.extra,
                payload=carried_b64,
                source="computer_execute",
                attached=True,
                visual_view=view_id,
                pointer_x=pointer_xy[0] if pointer is not None else None,
                pointer_y=pointer_xy[1] if pointer is not None else None,
                pointer_state=pointer_state if pointer is not None else None,
                last_click=pointer_fields.get("last_click") if pointer_fields else None,
            )
        )
        if pointer_fields is not None:
            # WP-C3: the additive pointer/last_click keys ride the image-bearing
            # reply only (the payload JSON of the TextContent block).
            response.update(pointer_fields)
        # WP-E E3 / DELTA 1 m1: ``image_after_item`` attaches HERE — after the
        # text-mode return and the view-derive-failed returns above — so the
        # key rides ONLY replies that really carry the image (and its
        # registered frame).  When the call ran a batch, the attached image is
        # the post-action frame of the LAST executed item and the reply names
        # it (``image_after_item`` is ALWAYS present on a multi-item batch's
        # image-bearing reply, including the value 0 — DELTA 1 m2).
        if outcome.image_after_item is not None:
            response["image_after_item"] = outcome.image_after_item
        return _execute_response_blocks(
            response,
            frame=frame,
            full_resolution=full_resolution,
            view_id=view_id,
            view_payload=view_payload,
            pointer=pointer,
        )
    return response
