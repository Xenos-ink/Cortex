"""Direct-action controller: the ComputerUseAgent pipeline (master-mission section 6).

The closed-loop decide phase machine (``run``: OBSERVE -> DECIDE(model) -> ... ->
RECOVER/REPLAN) was removed with the removed-loop family; this module now carries the
LIVE per-action pipeline used by ``run_single`` (``computer_execute``)::

    GROUND -> VALIDATE(staleness) -> RISK/POLICY -> APPROVAL
            -> EXECUTE(stop-checked) -> RE-OBSERVE -> VERIFY

The doctrine bullets below still apply verbatim to that per-action pipeline.

Doctrines implemented here (binding):

- **TaskState-driven**: every status transition, counter, and ``termination_reason`` lives
  on :class:`~computer_use_mcp.state.TaskState`; every exit path terminates with an
  explicit reason (completed / failed_verification / blocked_safety / approval_exhausted /
  limit_exceeded / stopped_by_user / unrecoverable / provider_error).
- **Stop discipline (P0-C)**: the :class:`~computer_use_mcp.state.StopToken` is checked at
  the loop top, before each provider call, before validation capture, before execution,
  inside recovery/dismiss attempts, and during waits. ``TaskStopped`` maps to
  ``stopped_by_user`` with an ``emergency_stop`` audit event. The stop token is NEVER
  reachable from model output: provider decisions are data (an :class:`AgentDecision`),
  not control — nothing a decision carries can arm the token or alter policy.
- **Verification baseline**: the baseline for verification is ALWAYS the observation the
  action was grounded from (the pre-action observation), never an after-screenshot; the
  Wave-0 bug where retries shifted the baseline with the previous after-image is fixed.
  The pre-execution staleness capture checks screen IDENTITY (hwnd/monitor/dimensions/
  coordinate space), not pixels, and never becomes the verification baseline.
- **Uncertain is never success (P0-A)**: a ``verification`` outcome of ``uncertain``
  routes to recovery classification. The single documented exception: ``wait`` actions —
  a wait makes no semantic state claim (the screen may legitimately change or stay still
  during a wait), so ``uncertain`` for ``wait`` continues with an audited note.
- **Confidence separation (Goal.md section 6)**: model confidence
  (``AgentDecision.action.confidence``), grounding confidence
  (``GroundingResult.confidence``), execution success (``ExecutionResult.ok``), and
  verification confidence (``VerificationResult.confidence``) stay separate end-to-end
  and are carried in per-action results.
- **Provider fail-closed**: ANY provider exception becomes an audited failure, a consumed
  step, and a LOW_CONFIDENCE recovery classification — it never raises out of :meth:`run`.
- **Limits**: :class:`~computer_use_mcp.limits.LimitEnforcer` gates screenshots (rate),
  model calls, actions, retries, recovery attempts, context size, and task duration.
  ``LimitExceeded`` is audited and terminates the task cleanly (fail safely).

Verification-intent defaults (from ``action.expected_effect`` +
``ProviderDecision.verification_hint`` + action type):

- explicit hint that names a :class:`~computer_use_mcp.verification.VerificationKind`
  value -> that kind (criteria filled from the expected effect);
- ``type`` -> ``expected_text`` (the typed text must appear in OCR evidence);
- ``move`` -> ``predicate`` (deterministic cursor-at-target: after-cursor within 2 px
  of the requested point on both axes; missing cursor fields -> uncertain);
- ``focus_window`` -> ``window_state`` (the active window title must contain
  ``action.target``, case-insensitive — deterministic via active_window_info, never
  pixels);
- ``keypress``/``hotkey`` whose expected effect says open/launch/switch-to ->
  ``window_state``;
- everything else (click/double_click/drag/scroll/wait/hotkey without a launch-prefix
  effect) -> ``visual_change``; when an expected effect is stated, a change is REQUIRED
  (unchanged screen = failed); with no stated expectation pixels alone stay ambiguous
  (identical screen -> ``uncertain`` -> recovery).

PERF-004 loop-economics doctrines (additive; no gate, guarantee, or audit is removed):

- **Observe reuse (C1)**: the post-action capture becomes the next step's loop_top
  observation when available (3 -> 2 full captures per step; the reuse sidesteps the
  rate gate because no capture happens). The pre-execution staleness check stays a
  fresh, burst-exempt capture and is now validated DIGEST-FIRST: the grounding source's
  pixel digest is compared with the fresh capture's digest (``digest match`` = the
  screen is pixel-identical since grounding, so identity drift is provably impossible;
  ``mismatch`` = pixels changed and the full identity staleness validation decides —
  a mismatch is a hint, never a verdict). Every validator guarantee and audit event is
  preserved; the digest result is audited per validation.
- **Rate-gate semantics (C2)**: ``min_screenshot_interval_ms`` protects FRESH
  observations (loop_top on a new step, host-driven observes); intra-step verification
  captures (validate probe, post-action, P0-H revalidate) are burst-exempt but still
  recorded into the enforcer, so session-wide protection stays enforced and bounded.
- **Verification ladder (C3)**: for ``model_judge`` intents the deterministic tiers
  (window/process/text/predicate criteria derived from the action intent) run first,
  the pixel-diff supporting tier second, and the provider judge ONLY when both cheap
  tiers are inconclusive. A deterministic verdict always skips the judge.
- **Queued host actions (C7)**: ``run_single`` accepts trailing-optional
  ``follow_ups`` (max :data:`~computer_use_mcp.models.MAX_FOLLOW_UPS`). Every queue
  item passes the FULL independent pipeline exactly like a single action; the queue
  stops on safety rejection, approval requirement, validator/grounding rejection, or
  post-action PREMISE INVALIDITY (a queued item's staleness probe shows the attached
  window's identity — hwnd + title + bounds — changed or closed since the premise it
  was grounded from: true staleness. Ordinary pixel changes from earlier items do NOT
  stop the batch; ``CORTEX_QUEUE_STRICT_DIGEST=1`` restores the v0.5.6 whole-screen
  digest stop). An UNCERTAIN verification does not stop the queue
  (REM-B: uncertain is not failure), and neither does the ``failed`` verdict
  of an EXECUTED item (the input dispatched, the next item re-grounds from
  the fresh post-action capture, and the honest verdict rides the per-item entry;
  ``CORTEX_QUEUE_STRICT_VERIFY=1`` restores the strict stop-on-failed behavior).
"""

from __future__ import annotations

import inspect
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from .audit import AuditLogger, Metrics
from .backend import ComputerBackend, FocusDriftError
from .focus_guard import GuardVerdict, InterferenceGuard
from .grounding import GroundingRouter
from .interference import REFOCUS_HINT, InterferencePolicy, parse_interference
from .limits import LimitEnforcer, LimitExceeded, Limits
from .models import (
    MAX_FOLLOW_UPS,
    ActionSpec,
    ActionType,
    ExecutionResult,
    GroundedAction,
    GroundingResult,
    Observation,
    Point,
    VerificationResult,
    ok_from_result,
)
from .observation import ObservationEngine, digest_matches
from .safety import SafetyPolicy
from .state import StopToken, TaskState, TaskStopped
from .validator import (
    COORDINATE_ACTIONS,
    GroundingValidator,
    ProcessIdentityUnavailableError,
    ProcessNotAllowedError,
    ValidationOutcome,
    WindowIdentityUnavailableError,
    _exe_matches,
    _process_matches,
    _title_matches_allowlist,
)


def _process_matches_pattern(candidate: str, pattern: str) -> bool:
    """Case-insensitive, ``.exe``-tolerant process/exe-basename match (allowlist gate)."""
    normalize = lambda value: value.strip().casefold().removesuffix(".exe")
    return normalize(candidate) == normalize(pattern)
from .verification import (
    FOCUS_CHANGE_INTENT_FLAG,
    ScreenshotDiffStrategy,
    VerificationEngine,
    VerificationIntent,
    VerificationKind,
    deterministic_tiers,
)

try:  # E4 lands SafetyContext in parallel; absent -> policy called without context.
    from .safety import SafetyContext  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover - pre-E4 state
    SafetyContext = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

#: Maximum seconds the controller will WAIT for the screenshot-rate gate before failing.
_MAX_SCREENSHOT_WAIT_SECONDS = 2.0
_SCREENSHOT_POLL_SECONDS = 0.05
_LAUNCH_PREFIXES: tuple[str, ...] = ("open ", "launch ", "start ", "switch to ", "focus ")

#: Observe phases that are INTRA-STEP verification captures (PERF-004 C2): burst-exempt.
#: The rate gate protects fresh observations (loop_top, host-driven direct_request,
#: recovery re-observes); these phases capture within one logical action step and skip
#: the interval wait while still recording into the enforcer.
_INTRA_STEP_EXEMPT_PHASES = frozenset({"validate", "post_action", "revalidate"})

#: Cursor-at-target tolerance for the deterministic ``move`` verification predicate (px).
_CURSOR_TOLERANCE_PX = 2

# --- WP4a (ORVEX-2026-108): F2-1 mismatch veto + F2-3/E-02 late-repaint re-capture ------------

#: F2-1 — confidence an R-04 ``integrity=mismatch`` demotion lands at. A disagreement
#: about the FOCUSED CONTROL is a failure to prove, not a definitive failure of the
#: action, so the verdict becomes the abstain band and ``ok`` becomes False.
_TYPE_MISMATCH_DEMOTION_CONFIDENCE = 0.4

#: F2-3 — settle BEFORE the single late-repaint re-capture, in seconds. The
#: post-action capture lands 16-32 ms after dispatch and is rate-exempt
#: (:data:`_INTRA_STEP_EXEMPT_PHASES`), so a UI that repaints a millisecond later used
#: to be missed forever with no retry at all — the measured cliff was 0 ms -> verified,
#: 1 ms -> uncertain.  When the verdict is ``uncertain`` **specifically** because the
#: two frames were byte-identical, the agent waits this long and takes exactly ONE more
#: ``post_action`` capture, so the cliff moves from "1 ms" to "past the settle".
#: Sized after the measured dispatch->capture gap (16-32 ms) and the repo's own
#: ``TYPE_VERIFY_SETTLE_SECONDS = 0.05``.
_IDENTICAL_FRAME_RECHECK_SETTLE_SECONDS = 0.05

#: E-02 hard bound on the late-repaint recovery: exactly ONE re-capture, ever. A world
#: that repaints later than the settle stays ``uncertain`` — a single bounded retry,
#: never a poll loop.
_IDENTICAL_FRAME_RECHECK_LIMIT = 1

#: E-02 conditioning: the ONE late-repaint re-capture fires ONLY when a fresh frame
#: could plausibly carry NEW evidence — i.e. the intent states an ``expected_effect``
#: (the verdict is about a described transition, so a late repaint can still prove it)
#: OR the action class is repaint-prone (``type``/``scroll``: the screen keeps moving
#: after dispatch by the nature of the action). An effect-less ``click``/``move``/
#: ``keypress`` on a static screen performs ZERO extra captures — captures/action
#: stays at the WP0 baseline (3.0 on the fake backend).
_REPAINT_PRONE_ACTIONS = frozenset({ActionType.TYPE, ActionType.SCROLL})

# --- unified reply contract (ORVEX-2026-108 MASTER MISSION §1) --------------------------------

#: §1.2 marker kinds — action types that are MARKERS, not screen claims. ``done`` is
#: the documented completion marker ("it does NOT end the session") and ``wait`` makes
#: no semantic state claim either: the screen may legitimately change or stay still
#: during a wait, which is exactly why this module's doctrine has always carved ``wait``
#: out of "uncertain is never success". Neither kind has a target, a coordinate, an
#: effect to cause, or a screen transition to judge, so running them through the pixel
#: ladder could only ever abstain — a STRUCTURALLY GUARANTEED ``uncertain`` that a
#: driver reads as "this failed" and is then told never to re-issue.
_NO_OP_MARKER_KINDS = frozenset({ActionType.DONE, ActionType.WAIT})

#: §1.2 marker confidence: high, because nothing was claimed — it is confidence that
#: the MARKER was handled, not that the task's effect occurred.
_NO_OP_MARKER_CONFIDENCE = 1.0

#: §1.1 S-12: the closed ``ensure_app_outcome`` vocabulary (mirrors the
#: :data:`~computer_use_mcp.models.EnsureAppOutcome` Literal enum in models.py).
ENSURE_APP_OUTCOMES: frozenset[str] = frozenset(
    {
        "reattached",        # an existing instance was focused and attached
        "ambiguous",         # several instances matched; the driver disambiguates
        "not_found",         # nothing matched; nothing was launched
        "launched",          # a process was REALLY spawned server-side
        "launch_rejected",   # the needle failed validation; nothing spawned
        "launch_unresolved", # a valid needle resolved nowhere; nothing spawned
    }
)

#: The abstain-band confidence an ensure_app probe FAILURE (launch_rejected /
#: launch_unresolved) and the ambiguous outcome land at (§1.3 MAJOR-4: failed /
#: uncertain are never successes).
_ENSURE_APP_PROBE_FAILURE_CONFIDENCE = 0.3

#: Launch-failure suffixes a NO_INSTANCE payload may carry (v0.7.1 Defect B honest
#: payload contract); a payload with ``launched=`` is a spawn, never a probe failure.
#: (The probe-failure VERIFICATION demotion and its abstain-band confidence are WP4b
#: scope; the tokens are classified here already so the enum is complete.)
ENSURE_APP_PROBE_FAILURES: tuple[str, ...] = ("launch_rejected", "launch_unresolved")


def _ensure_app_probe_failure(message: str) -> str | None:
    """Which failure-valued ``ensure_app`` probe this message carries, if any.

    ``None`` means "not a launch failure" — either a plain ``NO_INSTANCE`` /
    ``AMBIGUOUS_INSTANCE`` probe (which ran, and whose emptiness is the answer), a
    ``REATTACHED`` payload (never a probe), or a message with no probe at all.
    """
    text = str(message or "")
    for token in ENSURE_APP_PROBE_FAILURES:
        if f"{token}=" in text:
            return token
    return None


def ensure_app_outcome(message: str) -> str | None:
    """The typed outcome of one ``ensure_app`` execution, or ``None`` if it is not one.

    Derived from the payload the backend returns, in one place, so a driver can switch
    on a field instead of parsing a string. ``None`` means "this message is not an
    ``ensure_app`` payload" — never a default arm, so an unrecognised shape stays
    visible rather than being filed under a plausible-looking value.

    ``launched=`` is the only proof a process was REALLY spawned, so it outranks a
    leading ``NO_INSTANCE`` token (the payload is built before the spawn is attempted).

    ``not_found`` deliberately covers BOTH "nothing matched and nothing was launched"
    and "a launch was refused by the session's policy or process allowlist": the
    payload does not distinguish which gate refused (the allowlist denial never reaches
    the backend at all — ``allow_launch`` is simply ``False``), and this classifier
    does not invent a value the payload cannot support.
    """
    text = str(message or "")
    # MINOR-a (VAL-WP1): the STRUCTURED prefixes are authoritative and are checked
    # FIRST — a REATTACHED/AMBIGUOUS payload embeds a window TITLE, which may itself
    # contain the substring `` launched=``; that substring proves a spawn ONLY inside
    # a NO_INSTANCE launch payload.
    if text.startswith("REATTACHED"):
        return "reattached"
    if text.startswith("AMBIGUOUS_INSTANCE"):
        return "ambiguous"
    if text.startswith("NO_INSTANCE"):
        if " launched=" in text:
            return "launched"
        failure = _ensure_app_probe_failure(text)
        if failure == "launch_rejected":
            return "launch_rejected"
        if failure == "launch_unresolved":
            return "launch_unresolved"
        return "not_found"
    if text.startswith("LAUNCHED"):
        return "launched"
    return None


def rejection_reasons(outcome: "SingleActionOutcome") -> list[str]:
    """The §1.1 gate-named refusal reasons, derived in ONE place (ORVEX-2026-108).

    Used by BOTH the flat ``computer_execute`` reply builder (server.py) and the
    rejection-kind ``follow_up_results[i]`` entries (:meth:`ComputerUseAgent._queue_entry`),
    so the two surfaces can never disagree. Pattern: the typed code NAMES ITSELF as the
    first reason — ``safety_denied: <msg>``, ``digest_surprise: <msg>`` — while a plain
    ``rejected`` outcome keeps its own gate-named reasons verbatim.

    MINOR-N1 (VAL-WP1-02): reasons exist ONLY on rejection kinds. Every other kind
    returns ``[]`` and its queue entry carries NO ``reasons`` key at all — exactly
    like the flat reply of the same kind (executed/approval/error replies never had a
    ``reasons`` key).
    """
    kind = outcome.kind
    if kind == "rejected":
        return list(outcome.reasons)
    if kind == "safety_denied":
        return [f"safety_denied: {outcome.message}"]
    if kind == "digest_surprise":
        return [f"digest_surprise: {outcome.message}"]
    return []


def execute_result_token(outcome: "SingleActionOutcome") -> str:
    """The §1.2 ``result`` token for one ``computer_execute`` outcome (flat or queue item).

    One derivation, used by BOTH the flat reply builder and every
    ``follow_up_results[i]`` entry, so the two surfaces can never disagree. Executed
    outcomes derive the token from their own fields (``dry_run`` marker, the
    ``no_op_marker`` verification method, the verification verdict); every non-executed
    kind is a gate refusal (``rejected``), an approval hold (``not_executed``), or an
    operation error (``failed``).
    """
    result = outcome.result
    if outcome.kind == "executed" and result is not None:
        if result.dry_run:
            return "dry_run"
        verification = result.verification
        if verification is not None and verification.verification_method == "no_op_marker":
            return "marker"
        if verification is not None:
            return verification.outcome  # "verified" | "failed" | "uncertain"
        return "uncertain"
    return {
        "rejected": "rejected",
        "safety_denied": "rejected",
        "digest_surprise": "rejected",
        "approval_required": "not_executed",
        "error": "failed",
    }.get(outcome.kind, "uncertain")

# --- capture-source integrity guard (v0.7.1 Defect A) ----------------------------------------
#
#: Consecutive byte-identical pre/post capture pairs (across DISTINCT screen-affecting
#: actions) after which the capture SOURCE itself is called suspect. Field evidence
#: (v0.7.0 live Blender session): a dead/frozen capture source returned byte-identical
#: frames for every action, so the pixel tier confidently reported
#: ``Mean pixel difference 0.000000`` forever — a verdict-shaped lie. The marker never
#: changes a verdict; it converts that silent stream into typed, actionable diagnostics.
_CAPTURE_SUSPECT_THRESHOLD = 3

#: Marker vocabulary — same uppercase family as TARGET_GONE / FOCUS_DRIFTED /
#: STUCK_MODIFIER / NO_INSTANCE. Generic by design (any frozen capture source: a
#: disconnected session, a protected desktop, a stale duplicator — never app-specific).
CAPTURE_SOURCE_SUSPECTED = "CAPTURE_SOURCE_SUSPECTED"

#: Action kinds whose execution makes no screen-affecting claim; their (naturally
#: static) capture pairs must never feed the suspicion counter.
_CAPTURE_PAIR_EXEMPT_KINDS = frozenset({ActionType.WAIT, ActionType.DONE})

# --- R-5 mechanical-speed knobs (ORVEX-CORTEX-056-LIVEFIX) -----------------------------------
#: Env knob name: ``CORTEX_VALIDATE_REUSE_MS`` — the freshness window (milliseconds)
#: within which a DIRECT action's premise capture (taken at the start of this very
#: tool call) is reused as the validate-phase observation instead of re-capturing the
#: identical screen microseconds later. The window is the staleness guard: past it the
#: pipeline re-captures exactly as before. ``0`` disables the reuse (legacy behavior).
#: Queued follow-ups NEVER reuse: their premise is a previous item's post-action
#: capture (genuinely older), so the fresh validate capture — and the strict
#: digest-surprise probe — keep running for every queued item.
VALIDATE_REUSE_MS_ENV = "CORTEX_VALIDATE_REUSE_MS"

#: Default reuse window (ms). Generous relative to the microseconds of pure
#: computation between the direct_request capture and the validate point (grounding +
#: guard binding only — no input is dispatched between them), yet bounded.
_VALIDATE_REUSE_MS_DEFAULT = 1500.0


def _validate_reuse_window_ms() -> float:
    """Resolve the R-5 validate-reuse freshness window (0 disables; garbage -> default)."""
    import os

    raw = os.environ.get(VALIDATE_REUSE_MS_ENV)
    if raw is None or not raw.strip():
        return _VALIDATE_REUSE_MS_DEFAULT
    try:
        value = float(raw)
    except ValueError:
        return _VALIDATE_REUSE_MS_DEFAULT
    return value if value >= 0 else _VALIDATE_REUSE_MS_DEFAULT


# --- queue strict-verify knob -------------------------------------------------------------
#: Env knob name: ``CORTEX_QUEUE_STRICT_VERIFY`` — set to exactly ``1`` to restore the
#: v0.5.5 stop-on-failed queue semantics (an EXECUTED item whose verification outcome is
#: ``failed`` stops the batch). Default (unset/any other value): the queue CONTINUES past
#: an executed item's failed verdict — the input physically dispatched and safety already
#: admitted it; the honest failed verdict (ok=False + evidence) still rides the per-item
#: ``follow_up_results`` entry. Read lazily at each queue decision (test-toggleable).
QUEUE_STRICT_VERIFY_ENV = "CORTEX_QUEUE_STRICT_VERIFY"


def _queue_strict_verify() -> bool:
    """Resolve the strict-verify escape hatch (only the exact string ``1`` enables)."""
    import os

    return os.environ.get(QUEUE_STRICT_VERIFY_ENV, "").strip() == "1"


# --- queue premise-identity knob -----------------------------------------------------------
#: Env knob name: ``CORTEX_QUEUE_STRICT_DIGEST`` — set to exactly ``1`` to restore the
#: v0.5.6 whole-screen digest-surprise stop for QUEUED follow-ups (any pixel change
#: since the item's premise stops the batch). Default (unset/any other value): the
#: per-item validity check governs — the attached window's identity (hwnd + title +
#: bounds) unchanged AND the normal per-item grounding still applying means the item
#: executes; a changed/closed attached window is true staleness and still stops
#: (digest_surprise). Live evidence: the whole-screen stop fired on EVERY batch
#: whose earlier items legitimately changed the screen (drawing/typing), forcing one
#: full model turn per action. Read lazily at each queue decision (test-toggleable
#: like CORTEX_DIFF_FAST / CORTEX_QUEUE_STRICT_VERIFY).
QUEUE_STRICT_DIGEST_ENV = "CORTEX_QUEUE_STRICT_DIGEST"


def _queue_strict_digest() -> bool:
    """Resolve the strict-digest escape hatch (only the exact string ``1`` enables)."""
    import os

    return os.environ.get(QUEUE_STRICT_DIGEST_ENV, "").strip() == "1"


def _queue_window_identity(observation: Observation) -> tuple[Any, ...] | None:
    """The attached-window identity tuple (hwnd, title, bounds), or ``None``.

    ``None`` means the observation carries NO window identity at all (a backend
    without identity reporting, or no foreground window) — the caller decides the
    policy for that case; the identity is never partially compared.
    """
    info = observation.active_window_info
    if info is None:
        return None
    return (
        info.hwnd,
        info.title or "",
        tuple(info.bounds) if info.bounds is not None else None,
    )


def _queue_premise_valid(premise: Observation, current: Observation) -> bool:
    """Per-item queue validity: is this queued item's premise still current?

    Default (``CORTEX_QUEUE_STRICT_DIGEST`` unset): VALID when the attached window's
    identity (hwnd + title + bounds) is UNCHANGED between the item's premise capture
    and the fresh validate capture — earlier queue items legitimately change pixels
    (drawing, typing, dialogs) without invalidating the window the driver is driving.
    Identity-unavailable asymmetry is true staleness: the attached window CLOSED
    (premise had identity, current has none) or APPEARED (the reverse) stops the
    batch. When NEITHER side carries identity (identity-less backends), the legacy
    whole-screen digest comparison governs so the protection never silently weakens.
    ``CORTEX_QUEUE_STRICT_DIGEST=1`` restores the v0.5.6 whole-screen digest stop.
    The response-side digest_surprise concept for the PRIMARY action is untouched.
    """
    if _queue_strict_digest():
        return digest_matches(premise, current)
    premise_id = _queue_window_identity(premise)
    current_id = _queue_window_identity(current)
    if premise_id is None or current_id is None:
        if premise_id != current_id:
            return False  # the attached window closed or appeared: true staleness
        return digest_matches(premise, current)  # identity-less backend: legacy probe
    return premise_id == current_id


def _cursor_predicate(
    action: GroundedAction,
) -> tuple[Callable[[Observation, Observation], bool | None], str]:
    """Build the deterministic cursor-at-target predicate for ``move`` actions (PERF-004 C3)."""
    point = action.point

    def _cursor_at_target(
        _before: Observation, after: Observation, point: Point | None = point
    ) -> bool | None:
        if point is None or after.cursor_x is None or after.cursor_y is None:
            return None
        return (
            abs(after.cursor_x - point.x) <= _CURSOR_TOLERANCE_PX
            and abs(after.cursor_y - point.y) <= _CURSOR_TOLERANCE_PX
        )

    return _cursor_at_target, "cursor_at_target"


# --- F2-1 / F2-3 (ORVEX-2026-108 WP4a) ---------------------------------------------------------


def _is_identical_frame_uncertain(result: VerificationResult) -> bool:
    """Whether this ``uncertain`` is specifically "the two frames were byte-identical".

    F2-3's trigger condition, decided from evidence ALREADY IN HAND: the visual tier
    computes ``CaptureProvenance`` (sha256 + byte size of each of the two frames it
    diffed) and the combined all-uncertain result carries it forward
    (``VerificationEngine._combine``), so "the screen did not move at all" is a free
    read.  ``None`` provenance (a deterministic tier decided first, a starved ladder)
    and a NON-identical pair (a sub-threshold change, which is a different evidence
    class) both return False — the re-capture is reserved for the case where one more
    frame could plausibly carry NEW evidence.
    """
    if result.outcome != "uncertain":
        return False
    provenance = result.capture_provenance
    if provenance is None:
        return False
    return (
        provenance.before_bytes == provenance.after_bytes
        and provenance.before_sha256 == provenance.after_sha256
    )


def _type_integrity_status(message: str) -> str | None:
    """The bare R-04 status token out of a ``type`` action message suffix.

    ``backend.py:_format_integrity_status`` appends ``integrity=<status>(<n>/<total>)``
    (plus optional ``healed=``) to the ``Executed type.`` message.  Returns ``None``
    when the message carries no integrity suffix at all (a non-type action, or a
    backend that does not run the R-04 reader).
    """
    text = str(message or "")
    marker = "integrity="
    if marker not in text:
        return None
    return text.split(marker, 1)[1].split()[0].split("(")[0] or None


def _apply_type_integrity_status(
    result: VerificationResult, message: str, *, changed: bool
) -> VerificationResult:
    """Let the R-04 read-back status annotate — and, on ``mismatch``, veto — a verdict.

    Two producers answer two different questions about one ``type`` action and their
    answers used to be able to contradict each other in the SAME payload:

    * R-04 (``backend.py:_execute_type``) asks the FOCUSED CONTROL — the authoritative,
      target-scoped reader — "does your read-back end with what we typed?".  It
      reports ``verified | partial | unverified | mismatch`` and deliberately refuses
      to repair a disagreement (a masked/transformed buffer would double-apply), so
      ``mismatch`` is a strong statement: the text is not in the control.
    * the agent's ladder (``verification.py:UiControlTextStrategy``) asks a WEAKER
      question — "does the needle appear in the window TITLE or any of the first 50
      UI-control name/value fields" — and a title that merely mirrors the typed text
      matched it, producing ``ok=True / outcome=verified / confidence 0.9`` beside
      ``integrity=mismatch(0/4)``.

    Decision D-2a: the strong, target-scoped read-back outranks the substring match, so
    ``mismatch`` is NOT ``verified``.  The status is added as an **additive** evidence
    line (and named in the note) so the driver learns WHY, not merely that it failed.

    **``unverified`` is deliberately NOT a veto** and that co-occurrence is
    intentional: "no read-back was possible" is an ABSENCE of evidence about the
    focused control, not a disagreement, so a title/control match is still a
    legitimate uncontradicted answer.  It is annotated on the same additive channel
    (the split is disclosed, never silent) and its verdict is untouched.
    """
    status = _type_integrity_status(message)
    if status is None:
        return result
    if status == "mismatch" and result.outcome == "verified":
        line = (
            f"type_integrity={status}: the FOCUSED CONTROL's read-back does not end "
            "with the typed text, so the window-title/UI-control match cannot stand "
            "as proof that the text landed."
        )
        return VerificationResult(
            outcome="uncertain",
            changed=changed,
            note=(
                "A window-title/UI-control match reported the typed text as found, but "
                f"the focused control's read-back says {status} — the strong, "
                "target-scoped read-back outranks a substring match, so the action is "
                "not verified. Re-observe and decide; a masked or transformed field "
                "may legitimately hold something else. Uncertain is never success. | "
                f"{line}"
            )[:900],
            confidence=min(result.confidence, _TYPE_MISMATCH_DEMOTION_CONFIDENCE),
            evidence=[*result.evidence, line],
            verification_method=result.verification_method,
            observation_id=result.observation_id,
            capture_provenance=result.capture_provenance,
        )
    if status in {"unverified", "partial"}:
        # Annotate only: the verdict is exactly what the ladder decided.
        line = (
            f"type_integrity={status}: the focused control's read-back "
            + (
                "could not be obtained, which is an absence of evidence about that "
                "control and does not contradict the verdict above."
                if status == "unverified"
                else "matched only part of the typed text; the verdict above is the "
                "ladder's own and is unchanged by this status."
            )
        )
        if line in result.evidence:
            return result
        return result.model_copy(
            update={
                "evidence": [*result.evidence, line],
                "note": f"{result.note} | {line}"[:900],
            }
        )
    return result


@dataclass
class SingleActionOutcome:
    """Result of one direct (client-supplied) action through the pipeline.

    ``kind`` selects the legacy ``computer_execute`` response shape: ``executed`` carries
    an :class:`ExecutionResult`, ``rejected`` carries validator ``reasons``,
    ``safety_denied``/``approval_required`` carry a policy message, ``error`` is the
    fail-closed catch-all. PERF-004 adds ``digest_surprise`` (a queued speculative
    action whose staleness probe shows the screen changed since its premise was
    captured — the queue stops before executing it).

    PERF-004 C7 (additive): when the call carried ``follow_ups``, the FIRST action's
    outcome stays the legacy response payload and the per-item queue results travel in
    ``follow_up_results`` (bounded dicts, heavy payloads stripped) with
    ``follow_ups_stopped_reason`` naming where the queue halted (``None`` = every item
    executed and verified).

    T8 (additive): ``interference_events`` carries the Interference Guard's structured
    event payloads observed for this action (MODAL_DIALOG/FOCUS_DRIFTED/...); the
    queue reads them for the named stops (``modal_dialog``/``focus_drifted``).

    ORVEX-2026-108 §1.1 K-05 (additive): on a mid-type focus-drift rejection where at
    least one chunk already landed in the (now foreign) field, ``partial_delivery``
    carries ``{"delivered_chars": N}`` — the count of characters actually dispatched
    before the abort. Absence means "nothing was dispatched".
    """

    kind: str
    result: ExecutionResult | None = None
    reasons: list[str] = field(default_factory=list)
    message: str = ""
    requires_approval: bool = False
    partial_delivery: dict[str, int] | None = None
    model_confidence: float | None = None
    grounding_confidence: float | None = None
    verification_confidence: float | None = None
    follow_up_results: list[dict[str, Any]] | None = None
    follow_ups_stopped_reason: str | None = None
    interference_events: list[str] | None = None
    #: WP7b DELTA 1 (VAL-WP7b-01 #3): the queue collects the Interference Guard's
    # events from EVERY item (not only the first outcome, which is the benign
    # transition move on the plan path) into this merged list; the flat reply keeps
    # publishing ``interference_events`` (item 0's) unchanged, while the plan-step
    # machinery publishes the merged list. ``None`` = the queue path did not run.
    follow_up_interference_events: list[str] | None = None


class ComputerUseAgent:
    """Closed-loop phase machine driving one backend through goals and direct actions.

    Backward-compatible constructor: ``ComputerUseAgent(backend, provider)`` keeps working
    (defaults construct the foundation-layer objects); the server wires per-session
    TaskState/StopToken/LimitEnforcer/AuditLogger/Metrics explicitly.
    """

    def __init__(
        self,
        backend: ComputerBackend,
        provider: Any,
        safety: Any | None = None,
        validator: GroundingValidator | None = None,
        verifier: VerificationEngine | None = None,
        *,
        session_id: str = "unknown",
        task: TaskState | None = None,
        stop: StopToken | None = None,
        limits: Limits | None = None,
        enforcer: LimitEnforcer | None = None,
        auditor: AuditLogger | None = None,
        metrics: Metrics | None = None,
        allowed_processes: list[str] | None = None,
        grounding: GroundingRouter | None = None,
        interference: InterferencePolicy | None = None,
    ) -> None:
        self.backend = backend
        self.provider = provider
        self.safety = safety if safety is not None else SafetyPolicy()
        self.validator = validator if validator is not None else GroundingValidator()
        self.verifier = verifier if verifier is not None else VerificationEngine()
        self.observation = ObservationEngine(backend)
        self.session_id = session_id
        self.task = task if task is not None else TaskState()
        self.stop_token = stop if stop is not None else StopToken()
        self.limits = (limits if limits is not None else Limits()).validate()
        self.enforcer = enforcer if enforcer is not None else LimitEnforcer(self.limits)
        self.auditor = auditor if auditor is not None else AuditLogger()
        self.metrics = metrics if metrics is not None else Metrics()
        self.allowed_processes = list(allowed_processes or [])
        self.grounding = grounding if grounding is not None else GroundingRouter()
        # T8 Interference Guard (protection upgrade; runs as ADDITIONAL gates — it can
        # only add rejections/annotations, never bypass an existing one). Defaults to
        # the A12 protective policy when the caller omits it.
        self.interference = interference if interference is not None else parse_interference(None)
        self.guard = InterferenceGuard(backend, self.interference, emit=self._audit_guard_event)
        # v0.7.1 Defect A: capture-source integrity guard state (O(1), per session).
        # ``_capture_identical_pairs`` counts CONSECUTIVE byte-identical pre/post
        # visual-tier capture pairs across DISTINCT screen-affecting actions; the
        # action-id/base pair makes the expected-text fallback re-verify replace its
        # own contribution instead of double-counting one action.
        self._capture_identical_pairs = 0
        self._capture_pairs_action_id: str | None = None
        self._capture_pairs_counter_base = 0

    def _audit_guard_event(self, event_type: str, **kwargs: Any) -> None:
        """Audit-adapter for the Interference Guard (audit failures never break control)."""
        self._audit(event_type, **kwargs)

    # ------------------------------------------------------------------ interference helpers (T8)

    def _guard_pre_dispatch(self, action: GroundedAction) -> GuardVerdict | None:
        """Run the Interference Guard's pre-dispatch checks; audit + metric on rejections."""
        verdict = self.guard.verify_pre_dispatch(action)
        if verdict is not None:
            self.metrics.incr("interference_events")
            if verdict.blocking:
                self.metrics.incr("interference_rejections")
        return verdict

    def _guard_rejection_outcome(self, verdict: GuardVerdict) -> SingleActionOutcome:
        """Structured ``rejected`` outcome carrying the guard's event payload + hints.

        ORVEX-2026-105 interference retention (agent side): ``interference_events`` is
        ADDITIVE here. A pre-dispatch blocking verdict never reaches the post-action
        assignment (nothing dispatched, so there is no ``after`` frame to inspect),
        which is why a pre-dispatch event could never surface on the execute/queue
        reply — ``server`` publishes ``outcome.interference_events`` verbatim — nor,
        from WP7, on the plan lease. ``reasons`` keeps its exact pre-existing content
        and order, so every existing reader is unaffected.
        """
        return SingleActionOutcome(
            kind="rejected",
            reasons=[verdict.event, *verdict.hints],
            message=verdict.message,
            interference_events=[verdict.event],
        )

    @staticmethod
    def _verified_reanchor(guard: InterferenceGuard, verification_outcome: str, after_window: Any) -> Any:
        """B10 (b): re-anchor the session AFTER a VERIFIED action moved the surface.

        The guard's own rules decide whether the new surface is followable (same-process
        dialog/launcher, launch out of a launcher surface, dead anchor); a foreign steal
        keeps the anchor so the next dispatch rejects with FOCUS_TAKEN_BY.
        """
        if verification_outcome != "verified":
            return None  # only VERIFIED successes may move the anchor
        window = getattr(after_window, "active_window_info", None)
        guard.reanchor_after_success(window)
        return guard.bound

    def _bind_guard_from_observation(self, observation: Observation | None) -> None:
        """Arm the session's focus binding from an allowlisted observation (dormant otherwise)."""
        try:
            self.guard.maybe_bind(observation, self.allowed_processes)
        except Exception:
            logger.debug("guard binding failed", exc_info=True)

    def _guard_focus_hook(self, action: GroundedAction) -> Callable[[], None] | None:
        """Per-chunk focus-continuity hook for ``type`` actions (T8 mechanism iv)."""
        if action.action is not ActionType.TYPE:
            return None

        def _hook() -> None:
            self.guard.verify_mid_type(action)

        return _hook

    def _backend_execute(self, action: GroundedAction, stop: StopToken | None) -> str:
        """Backend execute seam with the T8 kwargs passed ONLY when they apply.

        Legacy call shape preserved for every action that needs neither the per-chunk
        focus hook (armed guard + ``type``) nor the ensure_app launch gate — the many
        valid legacy backend/fake implementations with the two-argument signature keep
        working unchanged.
        """
        focus_hook = (
            self._guard_focus_hook(action)
            if self.interference.focus_continuity.enabled and self.guard.armed
            else None
        )
        if focus_hook is None and action.action is not ActionType.ENSURE_APP:
            return self.backend.execute(action, stop)
        return self.backend.execute(
            action,
            stop,
            focus_hook=focus_hook,
            allow_launch=self._ensure_app_allow_launch(action),
        )

    def _ensure_app_allow_launch(self, action: GroundedAction) -> bool:
        """Whether THIS ensure_app may spawn a process (T8 mechanism ii policy gate).

        Server-side launching requires the ``attach_or_launch.launch="server"`` policy
        (the REM-B DEFAULT — an explicitly requested ``launch="driver"`` policy or the
        ``CORTEX_ATTACH_OR_LAUNCH=driver`` env knob restores the old never-launch
        default) AND — when a process allowlist is configured — the target process
        being allowlisted. Approval semantics, StopToken, and limits are unchanged.
        """
        if action.action is not ActionType.ENSURE_APP:
            return False
        if not self.interference.attach_or_launch.enabled:
            return False
        if self.interference.attach_or_launch.launch != "server":
            return False
        process = (action.target or "").split("|", 1)[0].strip()
        if self.allowed_processes and not any(
            _process_matches_pattern(process, pattern) for pattern in self.allowed_processes
        ):
            self._audit(
                "interference",
                action=action,
                result="ensure_app_launch_denied",
                metadata={"reason": "target process is not in the configured process allowlist"},
            )
            return False
        return True

    @staticmethod
    def _ensure_app_probe_outcome(message: str) -> bool:
        """True when an ensure_app execution returned a PROBE outcome (no screen claim).

        WP4b (C8 / dev F3 fix 5.1): ``LAUNCHED`` joins the probe vocabulary. A
        successful server-side launch is exactly as screen-claim-free as
        ``NO_INSTANCE`` — the payload IS the answer, and its window may not even
        exist yet when the post-action capture lands. Recognising it here keeps the
        probe shortcut's ``ok`` semantics UNCHANGED by the leading-token fix (that is
        §1.3, not 5.1); the two FAILURE-valued launches still start with
        ``NO_INSTANCE`` and are demoted by the probe branch below.
        """
        text = str(message or "")
        return text.startswith(("NO_INSTANCE", "AMBIGUOUS_INSTANCE", "LAUNCHED"))

    def _post_action_guard_events(
        self,
        action: GroundedAction,
        after: Observation | None,
    ) -> tuple[list[str], list[str], list[str]]:
        """Run the post-action guard checks; returns (events, annotations, stop_reasons)."""
        events: list[str] = []
        annotations: list[str] = []
        stop_reasons: list[str] = []
        try:
            verdicts = self.guard.post_action_events(action, after)
        except Exception:  # noqa: BLE001 - sentinel failures never break the pipeline
            return events, annotations, stop_reasons
        for verdict in verdicts:
            self.metrics.incr("interference_events")
            events.append(verdict.event)
            annotations.append(f"{verdict.message} {verdict.event}")
            if verdict.stop_reason:
                stop_reasons.append(verdict.stop_reason)
        return events, annotations, stop_reasons

    # ------------------------------------------------------------------ audit helpers

    def _active_app(self, observation: Observation | None) -> str | None:
        """Process name of the active window (title fallback) for audit events."""
        if observation is None:
            return None
        info = observation.active_window_info
        if info is not None and info.process_name:
            return info.process_name
        return observation.active_window

    def _audit(
        self,
        event_type: str,
        *,
        observation: Observation | None = None,
        action: GroundedAction | None = None,
        result: str | None = None,
        duration_ms: float | None = None,
        metadata: dict[str, Any] | None = None,
        **extra: Any,
    ) -> None:
        """Emit one audit event; audit failures never break the control loop."""
        merged: dict[str, Any] = dict(metadata or {})
        merged.update(extra)
        try:
            self.auditor.emit(
                str(event_type),
                self.session_id,
                task_id=self.task.task_id,
                observation_id=observation.observation_id if observation is not None else None,
                action_id=action.action_id if action is not None else None,
                active_app=self._active_app(observation),
                result=result,
                duration_ms=duration_ms,
                metadata={k: v for k, v in merged.items() if v not in (None, "")},
            )
        except Exception:
            logger.debug("Audit write failed", exc_info=True)

    # ------------------------------------------------------------------ phase helpers

    def _observe(self, phase: str) -> Observation:
        """OBSERVE phase: capture with audit + metrics (rate-gated per PERF-004 C2).

        Fresh observations (every phase outside :data:`_INTRA_STEP_EXEMPT_PHASES`) wait
        behind ``min_screenshot_interval_ms`` exactly as before. Intra-step verification
        captures are burst-exempt: they skip the wait but are still recorded into the
        enforcer (count + pacing timestamp), keeping session-wide protection truthful.
        """
        gated = phase not in _INTRA_STEP_EXEMPT_PHASES
        started = time.perf_counter()
        if gated:
            waited = 0.0
            while not self.enforcer.can_screenshot():
                if waited > _MAX_SCREENSHOT_WAIT_SECONDS:
                    raise LimitExceeded(
                        "min_screenshot_interval_ms",
                        (
                            f"Screenshot rate budget exhausted: interval "
                            f"{self.limits.min_screenshot_interval_ms}ms needs more than "
                            f"{_MAX_SCREENSHOT_WAIT_SECONDS:.0f}s of waiting."
                        ),
                    )
                if self.stop_token.wait(_SCREENSHOT_POLL_SECONDS):
                    self.stop_token.ensure_live()
                waited += _SCREENSHOT_POLL_SECONDS
        observation = self.observation.capture()
        try:  # R-5: capture-time freshness clock for the validate-phase reuse window
            observation._captured_monotonic = time.perf_counter()
        except Exception:  # noqa: BLE001 - PrivateAttr stamping must never break capture
            pass
        if gated:
            self.enforcer.record_screenshot()
        else:
            self.enforcer.record_burst_screenshot()
        self.metrics.incr("screenshot_count")
        duration_ms = (time.perf_counter() - started) * 1000.0
        self.metrics.record_latency("observation_ms", duration_ms)
        self.task.record_observation_id(observation.observation_id)
        self._audit(
            "observation",
            observation=observation,
            result="ok",
            duration_ms=duration_ms,
            phase=phase,
            gated=gated,
        )
        return observation

    def _ground(self, action: GroundedAction, observation: Observation) -> GroundingResult:
        """GROUND phase: route grounding, attach the result, bind the observation source."""
        grounding = self.grounding.route(action, observation)
        action.grounding = grounding
        if action.action in COORDINATE_ACTIONS:
            action.source_observation_id = observation.observation_id
        self._audit(
            "grounding",
            observation=observation,
            action=action,
            result="ok",
            metadata={
                "strategy": grounding.strategy,
                "grounding_confidence": grounding.confidence,
                "normalized": grounding.normalized,
            },
        )
        return grounding

    def _build_safety_context(self, observation: Observation | None) -> Any | None:
        """Build E4's pinned ``SafetyContext`` when the parallel landing provides it."""
        if SafetyContext is None:
            return None
        info = observation.active_window_info if observation is not None else None
        kwargs = {
            "goal": self.task.goal,
            "active_window_info": info,
            "active_process_name": info.process_name if info is not None else None,
            "window_title": (info.title if info is not None else None)
            or (observation.active_window if observation is not None else None),
            "task_summary": (self.task.goal or "")[:200],
            "recent_actions": [record.action_type for record in list(self.task.action_history)[-5:]],
        }
        try:
            return SafetyContext(**kwargs)
        except Exception:  # noqa: BLE001 - context construction must never break control
            return None

    def _denied_decision(self, reason: str) -> Any:
        """Fail-closed SafetyDecision-compatible object when the policy itself fails."""
        try:
            from .safety import SafetyDecision

            return SafetyDecision(allowed=False, requires_approval=False, reason=reason)
        except Exception:  # noqa: BLE001 - E4 field drift tolerated via shim
            return SimpleNamespace(allowed=False, requires_approval=False, reason=reason)

    def _evaluate_safety(self, action: GroundedAction, state: Any, observation: Observation | None) -> Any:
        """RISK/POLICY phase: contextual evaluation; a failing policy denies (fail-closed)."""
        context = self._build_safety_context(observation)
        try:
            if context is not None:
                try:
                    return self.safety.evaluate(action, state, context)
                except TypeError:
                    pass  # pre-E4 policy signature without context
            return self.safety.evaluate(action, state)
        except Exception as exc:  # noqa: BLE001 - policy failure denies, never crashes
            return self._denied_decision(f"Safety evaluation failed; failing closed: {exc}")

    def _build_intent(
        self,
        action: GroundedAction,
        verification_hint: str | None,
        expected_effect: str | None,
    ) -> VerificationIntent:
        """Build the semantic verification intent for an action (see module docstring)."""
        hint = (verification_hint or "").strip().casefold()
        effect = (expected_effect or action.expected_effect or "").strip() or None
        valid_kinds = {kind.value for kind in VerificationKind}
        kind = hint if hint in valid_kinds else None
        expected_text: str | None = None
        window_title: str | None = None
        window_title_match = "contains"  # case-insensitive substring (WindowStateStrategy)
        process_name: str | None = None
        expected_change: bool | None = None
        predicate: Callable[[Observation, Observation], bool | None] | None = None
        predicate_name: str | None = None
        if kind is None:
            if action.action is ActionType.TYPE:
                kind = VerificationKind.EXPECTED_TEXT.value
            elif action.action is ActionType.MOVE:
                # Deterministic cursor-at-target predicate: verified when the after
                # cursor sits within tolerance of the requested point on both axes.
                # Missing cursor fields yield None -> uncertain (never false success).
                kind = VerificationKind.PREDICATE.value
                predicate, predicate_name = _cursor_predicate(action)
            elif action.action in {ActionType.KEYPRESS, ActionType.HOTKEY} and effect:
                folded = effect.casefold()
                for prefix in _LAUNCH_PREFIXES:
                    if folded.startswith(prefix) and len(effect) > len(prefix):
                        kind = VerificationKind.WINDOW_STATE.value
                        window_title = effect[len(prefix):].strip() or None
                        break
            elif action.action is ActionType.FOCUS_WINDOW:
                # Deterministic window identity via active_window_info, never pixels;
                # the title match is case-insensitive "contains" (WindowStateStrategy).
                kind = VerificationKind.WINDOW_STATE.value
                window_title = action.target
            elif action.action is ActionType.ENSURE_APP:
                # T8: a REATTACHED ensure_app outcome is verified by the foreground
                # PROCESS identity (probe outcomes short-circuit before verification).
                kind = VerificationKind.PROCESS_STATE.value
                process_name = (action.target or "").split("|", 1)[0].strip() or None
            if kind is None:
                kind = VerificationKind.VISUAL_CHANGE.value
        elif kind == VerificationKind.MODEL_JUDGE.value:
            # PERF-004 C3: a model-judge intent ALSO carries the deterministic criteria
            # the action itself implies, so the cheap-first verification ladder
            # (deterministic tiers -> pixel diff -> judge) can skip the judge whenever
            # a deterministic strategy already reaches a verdict. The kind stays
            # model_judge: when every cheap tier is inconclusive the judge still runs.
            if action.action is ActionType.TYPE:
                # R-01: the deterministic text needle for a TYPE action is the TYPED
                # TEXT itself — the string that actually lands in the field's value —
                # not the effect prose. Searching the prose made the deterministic
                # ui_control_text tier abstain on EVERY stated-effect type action and
                # escalated them all to the pixel band; searching the typed text lets
                # the deterministic tier decide (verified when the text is visible in
                # the window title or a control's name/value) with the pixel tier kept
                # as the escalation for genuinely invisible content (canvas, grid).
                expected_text = action.text or effect
            elif action.action is ActionType.MOVE:
                predicate, predicate_name = _cursor_predicate(action)
            elif action.action in {ActionType.KEYPRESS, ActionType.HOTKEY} and effect:
                folded = effect.casefold()
                for prefix in _LAUNCH_PREFIXES:
                    if folded.startswith(prefix) and len(effect) > len(prefix):
                        window_title = effect[len(prefix):].strip() or None
                        break
            elif action.action is ActionType.FOCUS_WINDOW:
                window_title = action.target
        if kind == VerificationKind.EXPECTED_TEXT.value:
            # R-01 deterministic-first default for TYPE: the typed text is the primary
            # needle (see the model-judge branch comment); every other expected-text
            # source keeps the effect-first preference. The tier's absence verdict is
            # UNCERTAIN (never failed), so an invisible needle can only escalate.
            if action.action is ActionType.TYPE:
                expected_text = action.text or effect
            else:
                expected_text = effect or action.text
        elif kind == VerificationKind.WINDOW_STATE.value:
            window_title = window_title or effect
        elif kind == VerificationKind.PROCESS_STATE.value:
            process_name = process_name or effect
        elif kind == VerificationKind.VISUAL_CHANGE.value and effect:
            expected_change = True
            # REM-B (H2c): a CLICK/DOUBLE_CLICK with a stated effect is a focus-type
            # expectation ("Hex input focused") — flag it so the deterministic
            # FocusChangeStrategy tier (UIA focused element / window identity /
            # corroborated digest) runs BEFORE the pixel-diff tier.
            # does NOT widen this flag: FocusChangeStrategy's transition signals stay
            # click-scoped (generalizing them let a mere window change "verify" a
            # stated type effect — a false success the fault-injection pin catches).
            # The D11 doctrine lives in ScreenshotDiffStrategy: ANY intent carrying a
            # stated expected_effect degrades a zero/sub-threshold diff to
            # ``uncertain`` instead of a definitive false ``failed`` (keypress Enter
            # committing a shape with sub-threshold dashed handles). Unstated
            # expectations are untouched.
            if action.action in {ActionType.CLICK, ActionType.DOUBLE_CLICK}:
                metadata: dict[str, Any] = {
                    "action_id": action.action_id,
                    "verification_hint": hint,
                    FOCUS_CHANGE_INTENT_FLAG: True,
                }
                return VerificationIntent(
                    kind=kind,
                    expected_text=expected_text,
                    expected_window_title=window_title,
                    window_title_match=window_title_match,
                    expected_process_name=process_name,
                    expected_change=expected_change,
                    expected_effect=effect,
                    predicate=predicate,
                    predicate_name=predicate_name,
                    metadata=metadata,
                )
        return VerificationIntent(
            kind=kind,
            expected_text=expected_text,
            expected_window_title=window_title,
            window_title_match=window_title_match,
            expected_process_name=process_name,
            expected_change=expected_change,
            expected_effect=effect,
            predicate=predicate,
            predicate_name=predicate_name,
            metadata={"action_id": action.action_id, "verification_hint": hint},
        )

    def _focus_allowlist_rejection(
        self, action: GroundedAction, state: Any | None = None
    ) -> ValidationOutcome | None:
        """Agent-level focus allowlist gate (fail-closed), WRONG_WINDOW-shaped.

        ``focus_window`` resolves its target window BEFORE execution (the backend must
        not run at all for a disallowed target), so neither allowlist can be checked
        from the active-window observation the way other actions do. When the action is
        a focus_window, the target window is looked up through the backend and checked
        against BOTH configured allowlists:

        - process allowlist (``self.allowed_processes``): window not found -> code
          ``process_identity_unavailable`` (fail closed); found but its process/exe is
          outside the allowlist (validator matching semantics) -> code
          ``process_not_allowed``;
        - window-title allowlist (``state.allowed_windows`` — the same effective
          allowlist the validator enforces on the ACTIVE window): window not found ->
          code ``window_identity_unavailable`` (fail closed); found but its title is
          outside the allowlist (casefolded exact-or-substring, mirroring
          ``GroundingValidator._window_allowed``) -> code ``window_not_allowed``.

        The rejection reuses the validator's rejection codes and typed errors so
        callers get the same rejection shape and recovery mapping
        (``FailureClass.WRONG_WINDOW``) as ordinary allowlist violations. Returns
        ``None`` (gate skipped) when neither allowlist is configured or the action is
        not a focus_window.
        """
        if action.action is not ActionType.FOCUS_WINDOW:
            return None
        target = (action.target or "").strip()
        if self.allowed_processes:
            process_rejection = self._focus_process_allowlist_rejection(target)
            if process_rejection is not None:
                return process_rejection
        allowed_windows = list(getattr(state, "allowed_windows", None) or [])
        if allowed_windows:
            candidate = self.backend.find_window_by_title(target)
            if candidate is None:
                outcome = ValidationOutcome(
                    valid=False,
                    reasons=[
                        (
                            "focus_window target window could not be resolved while a "
                            "window-title allowlist is configured; failing closed."
                        )
                    ],
                    codes=["window_identity_unavailable"],
                )
                outcome._error = WindowIdentityUnavailableError(
                    f"Window {target!r} could not be resolved; cannot verify the window-title allowlist."
                )
                return outcome
            if not _title_matches_allowlist(candidate.title, allowed_windows):
                outcome = ValidationOutcome(
                    valid=False,
                    reasons=[
                        (
                            f"Target window title {candidate.title!r} is not in the "
                            "configured window allowlist."
                        )
                    ],
                    codes=["window_not_allowed"],
                )
                return outcome
        return None

    def _focus_process_allowlist_rejection(self, target: str) -> ValidationOutcome | None:
        """Process-allowlist half of the focus gate (semantics unchanged)."""
        candidate = self.backend.find_window_by_title(target)
        if candidate is None:
            outcome = ValidationOutcome(
                valid=False,
                reasons=[
                    (
                        "focus_window target window could not be resolved while a process "
                        "allowlist is configured; failing closed."
                    )
                ],
                codes=["process_identity_unavailable"],
            )
            outcome._error = ProcessIdentityUnavailableError(
                f"Window {target!r} could not be resolved; cannot verify the process allowlist."
            )
            return outcome
        allowed = any(
            (candidate.process_name and _process_matches(candidate.process_name, pattern))
            or (candidate.exe_path and _exe_matches(candidate.exe_path, pattern))
            for pattern in self.allowed_processes
        )
        if allowed:
            return None
        identity = candidate.process_name or candidate.exe_path
        if identity is None:
            outcome = ValidationOutcome(
                valid=False,
                reasons=[
                    (
                        "focus_window target window carries no process identity; the process "
                        "allowlist cannot be verified."
                    )
                ],
                codes=["process_identity_unavailable"],
            )
            outcome._error = ProcessIdentityUnavailableError(
                f"Window {target!r} has no process identity; cannot verify the process allowlist."
            )
            return outcome
        outcome = ValidationOutcome(
            valid=False,
            reasons=[
                f"Target window process {identity!r} is not in the configured process allowlist."
            ],
            codes=["process_not_allowed"],
        )
        outcome._error = ProcessNotAllowedError(
            f"Target window process {identity!r} is not in the configured process allowlist."
        )
        return outcome

    def _verifier_has_judge(self) -> bool:
        """True when the injected verifier chain carries a synchronous model judge."""
        for strategy in getattr(self.verifier, "strategies", []) or []:
            if getattr(strategy, "judge", None) is not None:
                return True
        return False

    async def _provider_judge(
        self, intent: VerificationIntent, before: Observation, after: Observation
    ) -> VerificationResult:
        """MODEL_JUDGE via the pinned ``provider.judge_change`` callback, fail-closed."""
        judge = getattr(self.provider, "judge_change", None)
        if not callable(judge):
            return VerificationResult(
                outcome="uncertain",
                changed=False,
                note="No provider judge_change is available; model-based verification is unavailable.",
                confidence=0.0,
                verification_method="model_visual",
                observation_id=after.observation_id,
            )
        try:
            # H1 (master-mission Phase 2 / REM-A): the observations already carry
            # PNG base64 — reuse those encoded bytes verbatim instead of the old
            # redundant decode -> PIL -> re-encode roundtrip. No verification-semantics
            # change: the judge receives the same image bytes as before.
            before_b64 = before.image_base64
            after_b64 = after.image_base64
            raw = judge(before_b64, after_b64, intent.expected_effect or "")
            if inspect.isawaitable(raw):
                raw = await raw
        except Exception as exc:  # noqa: BLE001 - judge failure degrades to uncertain
            return VerificationResult(
                outcome="uncertain",
                changed=False,
                note=f"Provider judge failed: {type(exc).__name__}.",
                confidence=0.0,
                verification_method="model_visual",
                observation_id=after.observation_id,
            )
        data = raw if isinstance(raw, dict) else {
            key: getattr(raw, key, None) for key in ("outcome", "confidence", "reason")
        }
        outcome = str(data.get("outcome", "uncertain")).casefold()
        if outcome not in {"verified", "failed", "uncertain"}:
            outcome = "uncertain"
        try:
            confidence = min(max(float(data.get("confidence") or 0.0), 0.0), 1.0)
        except (TypeError, ValueError):
            confidence = 0.0
        note = str(data.get("reason") or data.get("note") or "")[:500]
        return VerificationResult(
            outcome=outcome,
            changed=outcome == "verified",
            note=note or f"Provider judge outcome: {outcome}.",
            confidence=confidence,
            verification_method="provider_judge",
            observation_id=after.observation_id,
        )

    async def _verify_judge_ladder(
        self, intent: VerificationIntent, before: Observation, after: Observation
    ) -> tuple[VerificationResult, str]:
        """Cheap-first verification ladder for model-judge intents (PERF-004 C3).

        Tier 1 — deterministic strategies for the criteria the action intent states
        (window identity, process identity, expected text, predicates): a definitive
        verdict here SKIPS the judge entirely. Tier 2 — the pixel-diff supporting
        check, which can only definitively FAIL a judge intent (a stated expectation
        the pixels already falsify; it can never upgrade to verified). Tier 3 — the
        model judge (injected chain judge or the provider ``judge_change`` callback),
        reached only when both cheap tiers are inconclusive. Uncertain is never
        success at any tier.
        """
        for strategy, sub_intent in deterministic_tiers(intent):
            try:
                result = strategy.verify(sub_intent, before, after)
            except Exception as exc:
                logger.debug("Ladder deterministic tier %s failed", strategy.name, exc_info=exc)
                continue
            if result.outcome in {"verified", "failed"}:
                return result, "deterministic"
        if after.image_base64 != before.image_base64 or after.observation_id != before.observation_id:
            try:
                diff_result = ScreenshotDiffStrategy().verify(intent, before, after)
            except Exception:  # noqa: BLE001 - diff failure degrades to the judge tier
                diff_result = None
            if diff_result is not None and diff_result.outcome == "failed":
                return diff_result, "pixel_diff"
        else:
            # B1: the post-action capture is the SAME capture as the grounding source
            # (a starved ladder — e.g. the response screenshot was omitted and no fresh
            # verification capture exists). A self-comparison 0.0-diff is NOT evidence
            # of no-change; route to the next tier instead of false-failing.
            logger.debug("Ladder skipped pixel-diff tier: after-capture is the grounding capture")
        if self._verifier_has_judge():
            return self.verifier.verify(intent, before, after), "model_judge"
        return await self._provider_judge(intent, before, after), "model_judge"

    def _capture_source_guard(
        self, action: GroundedAction | None, result: VerificationResult
    ) -> tuple[VerificationResult, str | None]:
        """v0.7.1 Defect A: detect a dead/frozen capture source and say so honestly.

        Consumes the visual tier's ``capture_provenance`` (hash+size of the pre/post
        frames ALREADY diffed — no new captures): a byte-identical pair feeds a
        per-session counter of consecutive identical pairs across DISTINCT
        screen-affecting actions; any pair whose hashes/bytes differ RESETS it;
        pairs without provenance (a deterministic tier decided first, a starved
        ladder) and exempt kinds (``wait``/``done`` — and ensure_app probe results,
        which never reach this method with provenance) leave it untouched.

        At :data:`_CAPTURE_SUSPECT_THRESHOLD` consecutive identical pairs the result
        is re-issued with the typed marker ``CAPTURE_SOURCE_SUSPECTED
        identical_pairs=<n>`` appended to note+evidence, and the marker string is
        returned for the verification audit metadata. THE VERDICT ITSELF IS NEVER
        ALTERED — ``verified``/``failed``/``uncertain`` stay exactly what the
        evidence honestly supports (fail-open on the verdict, fail-loud in
        diagnostics). The expected-text fallback re-verifies the SAME action; the
        stored counter base makes that re-verification replace its own contribution
        so one action counts at most one pair.
        """
        provenance = result.capture_provenance
        if provenance is None or action is None or action.action in _CAPTURE_PAIR_EXEMPT_KINDS:
            return result, None
        if action.action_id != self._capture_pairs_action_id:
            self._capture_pairs_action_id = action.action_id
            self._capture_pairs_counter_base = self._capture_identical_pairs
        else:
            # same action re-verified (expected-text fallback): undo the previous
            # contribution before re-applying, so the pair is counted exactly once.
            self._capture_identical_pairs = self._capture_pairs_counter_base
        identical = (
            provenance.before_bytes == provenance.after_bytes
            and provenance.before_sha256 == provenance.after_sha256
        )
        if identical:
            self._capture_identical_pairs += 1
        else:
            self._capture_identical_pairs = 0
        if self._capture_identical_pairs < _CAPTURE_SUSPECT_THRESHOLD:
            return result, None
        marker = f"{CAPTURE_SOURCE_SUSPECTED} identical_pairs={self._capture_identical_pairs}"
        annotated = result.model_copy(
            update={
                "note": result.note if marker in result.note else f"{result.note} | {marker}"[:900],
                "evidence": [*result.evidence, marker],
            }
        )
        return annotated, marker

    async def _verify(
        self,
        intent: VerificationIntent,
        before: Observation,
        after: Observation,
        action: GroundedAction | None = None,
        *,
        return_tier: bool = False,
    ) -> Any:
        """VERIFY phase: strategy chain or provider judge; uncertain never becomes success.

        DELTA 1 (VAL-WP4b MAJOR): the ``record`` parameter is REMOVED — this method
        NEVER emits the metrics/audit event. Every production caller (all three
        ``run_single`` branches) runs the ladder unrecorded and the ONE
        ``_record_verification`` after the demotions is the only verification audit
        record (E-05), so a hard-coded tier at the record site is pinned by the
        production-path test
        (``test_reply_contract.py::test_verification_audit_names_the_real_ladder_tier``).
        ``return_tier=True`` returns ``(result, ladder_tier)`` so that single record
        names the tier that actually decided.
        """
        ladder_tier = "engine"
        try:
            if intent.kind == VerificationKind.MODEL_JUDGE.value:
                result, ladder_tier = await self._verify_judge_ladder(intent, before, after)
            elif after.observation_id == before.observation_id:
                # B1: the after-capture IS the grounding capture (starved ladder) — a
                # self-comparison 0.0 pixel diff is not evidence of no-change, so the
                # screenshot-diff tier is routed past (next tiers decide; all-uncertain
                # stays uncertain, never a false failure).
                strategies = [
                    strategy
                    for strategy in self.verifier.strategies
                    if getattr(strategy, "name", "") != "screenshot_diff"
                ]
                result = self.verifier.verify(intent, before, after, strategies=strategies)
                ladder_tier = "engine_no_diff"
            else:
                result = self.verifier.verify(intent, before, after)
        except Exception as exc:  # noqa: BLE001 - verification failure is never success
            result = VerificationResult(
                outcome="uncertain",
                changed=False,
                note=f"Verification raised {type(exc).__name__}; treated as cannot-determine.",
                confidence=0.0,
                verification_method="controller_guard",
                observation_id=after.observation_id,
            )
        # v0.7.1 Defect A: honest capture-source diagnostics (marker rides ALONGSIDE
        # the verdict; the verdict itself is never altered). The single record
        # downstream re-runs this guard and carries ``capture_source_suspected``.
        result, _capture_marker = self._capture_source_guard(action, result)
        return (result, ladder_tier) if return_tier else result

    def _record_verification(
        self,
        result: VerificationResult,
        intent: VerificationIntent,
        after: Observation,
        action: GroundedAction | None,
        *,
        ladder_tier: str,
        duration_ms: float,
        capture_marker: str | None,
    ) -> None:
        """Emit the ONE metrics + audit record for one action's verification.

        Split out of :meth:`_verify` (F2-3) so a verification that runs the ladder more
        than once for a single action — the one late-repaint re-capture, the
        expected-text fallback — still produces exactly one audit event carrying the
        FINAL verdict, and exactly one latency sample for the whole verification. The
        audit ``result`` and the reply verdict are produced from the same object, and
        since E-05 (WP4b) the caller emits this record AFTER every demotion — the
        type-integrity mismatch veto, the launch-outcome demotion, and the post-action
        FOCUS_DRIFTED demotion — so the audit verdict ALWAYS equals the reply verdict
        on every executed-action path.
        """
        self.metrics.record_latency("verification_ms", duration_ms)
        if result.outcome == "verified":
            self.metrics.incr("verification_verified")
        elif result.outcome == "failed":
            self.metrics.incr("verification_failed")
        else:
            self.metrics.incr("verification_uncertain")
        provenance = result.capture_provenance
        self._audit(
            "verification",
            observation=after,
            action=action,
            result=result.outcome,
            duration_ms=duration_ms,
            metadata={
                "method": result.verification_method,
                "verification_confidence": result.confidence,
                "note": result.note[:200],
                "intent_kind": intent.kind,
                "ladder_tier": ladder_tier,
                # honest provenance on every verified-action audit event (None dropped)
                "capture_provenance": (
                    (
                        f"before={provenance.before_sha256[:12]}:{provenance.before_bytes} "
                        f"after={provenance.after_sha256[:12]}:{provenance.after_bytes}"
                    )
                    if provenance is not None
                    else None
                ),
                "capture_source_suspected": capture_marker,
            },
        )

    # ------------------------------------------------------------------ direct actions

    async def run_single(
        self,
        state: Any,
        action: GroundedAction,
        *,
        approved: bool = False,
        expected_effect: str | None = None,
        follow_ups: list[ActionSpec] | None = None,
        follow_ups_cap: int = MAX_FOLLOW_UPS,
        plan_policies: list[dict[str, Any] | None] | None = None,
        plan_context: dict[str, Any] | None = None,
    ) -> SingleActionOutcome:
        """Run one client-supplied action through the full pipeline (``computer_execute``).

        The action's ``confidence`` is the client-asserted MODEL confidence (legacy
        hardcoded 1.0); grounding confidence, staleness, risk, approval, and verification
        apply independently. Coordinate-bearing actions carry the source observation
        binding; staleness rejections trigger ONE automatic re-observe + re-validate
        (P0-H) before reporting rejection.

        PERF-004 C7 (trailing-optional): ``follow_ups`` queues up to
        :data:`~computer_use_mcp.models.MAX_FOLLOW_UPS` additional actions after the
        primary action. Each queue item passes the FULL independent pipeline
        (ground -> validate -> safety -> approval semantics -> execute -> verify)
        exactly like a single action; the queue stops at a safety rejection,
        approval requirement, validator/grounding rejection, or post-action digest
        surprise. An UNCERTAIN verification (no expectation stated / a non-visual
        action pixels cannot judge) does NOT stop the queue (REM-B H2b), and
        neither does the ``failed`` verdict of an EXECUTED item (the
        honest failed verdict rides the per-item entry while the batch continues;
        ``CORTEX_QUEUE_STRICT_VERIFY=1`` restores stop-on-failed). Every executed
        action emits its own audit events. ``approved`` applies to every item (the
        safety policy evaluates each independently).

        ORVEX-2026-101 Batch 2 (P2.1, additive; the ONLY queue changes the waypoint
        plan requires — WP7b wiring): ``follow_ups_cap`` lets the plan surface pass
        its own already-validated item count (a plan point step normalizes to up to 7
        items — move + 5 actions + the final wait — more than the speculative queue's
        ``MAX_FOLLOW_UPS``; the default keeps the legacy silent-truncation behavior
        byte-identical), and ``plan_policies`` carries the plan's point-end stop
        policies (RFC §2.3 T7/T8: a plan point resolved failed/uncertain honors the
        point's ``on_failure``/``on_uncertain`` — stop by default, a deliberate
        deviation from the exploratory queue's continue). When ``plan_policies`` is
        not None the queue path runs even for a single item so plan responses carry
        the uniform per-item aggregate.
        """
        if follow_ups or plan_policies is not None:
            specs = list(follow_ups or [])[:follow_ups_cap]
            return await self._run_action_queue(
                state,
                action,
                specs,
                approved=approved,
                expected_effect=expected_effect,
                plan_policies=plan_policies,
                plan_context=plan_context,
            )
        outcome, _post = await self._run_single_pipeline(
            state, action, approved=approved, expected_effect=expected_effect
        )
        return outcome

    # WP7b-SEAM(plan-queue) — WIRED: the plan path enters through the additive
    # ``plan_policies`` / ``plan_context`` parameters below. Per-policy settle
    # stamping (``item.plan_point_settle`` bool + ``plan_point_settle_ms`` value),
    # the TaskStopped partial-truth entry (``dispatched_unverified``), and the
    # point-end policy stop all live in ``_run_action_queue``; the LEASE-side
    # retention (interference events, K-53 cursor, prefix merge) stays in
    # server.py's step machinery, which is the only place the lease is in scope.
    async def _run_action_queue(
        self,
        state: Any,
        action: GroundedAction,
        follow_ups: list[ActionSpec],
        *,
        approved: bool,
        expected_effect: str | None,
        plan_policies: list[dict[str, Any] | None] | None = None,
        plan_context: dict[str, Any] | None = None,
    ) -> SingleActionOutcome:
        """Speculative host queue (PERF-004 C7): primary action + bounded follow_ups.

        SAFETY MANDATE — zero bypass: every item runs the complete independent pipeline
        (no state is shared between items except the fresh post-action observation that
        becomes the next item's grounding source — the observe-reuse doctrine). Stops:
        named interference stops (modal dialog / focus drift), safety rejection,
        approval requirement, validator rejection, PREMISE INVALIDITY (the
        attached window's identity changed or closed since the item's premise — true
        staleness; ordinary pixel changes from earlier items do not stop the batch, and
        ``CORTEX_QUEUE_STRICT_DIGEST=1`` restores the v0.5.6 whole-screen digest stop),
        stop token, limit trip, or a missing post-action observation. REM-B (H2b): an
        item whose verification is UNCERTAIN (no expectation stated / non-visual action
        the diff cannot judge) does NOT stop the queue — uncertain is not failure — but
        its per-item result keeps the honest uncertain verdict and ``ok=False``.
        an EXECUTED item whose verification outcome is ``failed`` also does
        NOT stop the queue by default — the input dispatched and the next item
        re-grounds from the fresh post-action capture; the honest failed verdict rides
        the per-item entry. ``CORTEX_QUEUE_STRICT_VERIFY=1`` restores the v0.5.5
        stop-on-failed behavior. R-18: an item that cannot even be CONVERTED to a
        :class:`GroundedAction` (target-less ``focus_window``, half-specified ``drag``,
        1-key ``hotkey``, ...) stops before ANY dispatch with the typed
        ``invalid_follow_up`` stop — zero items run, no raw pydantic
        ``ValidationError`` escapes (the MCP boundary maps the same condition onto the
        teaching ``invalid_action`` rejection).
        """
        queue_items: list[tuple[GroundedAction, str | None]] = [(action, expected_effect)]
        for position, spec in enumerate(follow_ups, start=1):
            try:
                grounded_spec = spec.to_grounded(reason_prefix="MCP follow_up action")
            except Exception as exc:  # noqa: BLE001 - R-18: never an uncaught ValidationError
                # The conversion loop precedes the pipeline loop, so NOTHING has
                # dispatched — the legacy fail-closed effect is preserved exactly;
                # only the error SURFACE changes: a typed rejected outcome (the server
                # boundary pre-validates the same condition and answers with the
                # teaching ``invalid_action`` shape; this guard covers direct
                # ``run_single`` callers with the same zero-uncaught guarantee).
                rejection = SingleActionOutcome(
                    kind="rejected",
                    reasons=[
                        (
                            f"follow_ups[{position}] ({spec.action.value}) is not a "
                            f"valid action: {exc}"
                        ),
                        (
                            "Valid shapes: focus_window needs a non-empty target window "
                            "title; drag needs x,y start AND x2,y2 end; hotkey needs 2-12 "
                            "key names (single keys belong on keypress); move needs x,y; "
                            "ensure_app needs a non-empty \"process[|doc-token]\" target."
                        ),
                    ],
                    message=(
                        f"Queue rejected before dispatch: follow_ups[{position}] "
                        f"({spec.action.value}) is not a valid action: {str(exc)[:180]}"
                    ),
                    follow_up_results=[
                        {
                            "index": position,
                            # VAL-WP1 carry-over: the hand-built pre-dispatch entry
                            # carries the SAME field set every pipeline-built entry
                            # has — ``action_id`` (nothing dispatched, so no action
                            # exists to own an id) and ``ensure_app_outcome`` (None)
                            # are filled as None so entry field-set parity holds
                            # across the whole ``follow_up_results`` surface (pinned
                            # by tests/test_reply_contract.py).
                            "action_id": None,
                            "action_type": spec.action.value,
                            "kind": "rejected",
                            "result": "rejected",
                            "ok": False,
                            "message": f"{type(exc).__name__}: {str(exc)[:200]}",
                            "reasons": ["invalid_action"],
                            "requires_approval": False,
                            "ensure_app_outcome": None,
                            "model_confidence": None,
                            "grounding_confidence": None,
                            "verification_confidence": None,
                        }
                    ],
                    follow_ups_stopped_reason="invalid_follow_up",
                )
                self._audit(
                    "queue",
                    action=action,
                    result="invalid_follow_up",
                    metadata={
                        "items": len(follow_ups) + 1,
                        "executed": 0,
                        "stopped_reason": "invalid_follow_up",
                        "invalid_index": position,
                        "invalid_action": spec.action.value,
                    },
                )
                return rejection
            queue_items.append((grounded_spec, spec.expected_effect))
        # ORVEX-2026-101 Batch 3 (P3.2/D15) + E-01 (WP7b): the plan's settle policy
        # rides the queue items as INTERNAL GroundedAction markers (never
        # host-visible: ActionSpec deliberately does not mirror them — P3.2 item 9).
        # The plan point's LAST item carries True plus the DECLARED ms — the backend
        # settles ONCE there, for exactly the stated duration, after that dispatch
        # and before its post-action observation; transition moves of points WITH
        # actions carry False (they settle not after the move). follow_ups queues
        # and single actions never reach this loop: their items keep the markers
        # unset (the exact legacy settle policy, byte-identical).
        if plan_policies is not None:
            for marker_index, (item, _item_effect) in enumerate(queue_items):
                if marker_index >= len(plan_policies):
                    break  # misaligned context: leave the legacy settle policy untouched
                policy = plan_policies[marker_index]
                item.plan_point_settle = policy is not None
                item.plan_point_settle_ms = policy["settle_ms"] if policy is not None else None

        async def _run_item_with_partial_truth(
            item_index: int,
            item: GroundedAction,
            item_effect: str | None,
            premise: Observation | None,
            *,
            strict_digest: bool,
        ) -> tuple[SingleActionOutcome, Observation | None]:
            try:
                return await self._run_single_pipeline(
                    state,
                    item,
                    approved=approved,
                    expected_effect=item_effect,
                    source_observation=premise,
                    strict_digest=strict_digest,
                )
            except TaskStopped as exc:
                if plan_context is not None and item._dispatch_started:
                    follow_up_results.append(
                        {
                            "index": item_index,
                            "action_id": item.action_id,
                            "action_type": item.action.value,
                            "kind": "dispatched_unverified",
                            # §1 envelope parity: the token comes from the same §1.2
                            # set — "dispatched, unproven" IS ``uncertain`` — and ok
                            # follows the shared ok-rule (never True here).
                            "result": "uncertain",
                            "ok": False,
                            "message": str(exc),
                            "reasons": ["stopped_after_dispatch"],
                            "requires_approval": False,
                            "ensure_app_outcome": None,
                            "model_confidence": item.confidence,
                            "grounding_confidence": None,
                            "verification_confidence": None,
                            "verification": {
                                "outcome": "uncertain",
                                "confidence": 0.0,
                                "evidence": [],
                                "verification_method": "none",
                            },
                            "verification_outcome": "uncertain",
                            "verification_note": (
                                "Input dispatched before cancellation; resulting state "
                                "was not independently verified."
                            ),
                        }
                    )
                raise
        follow_up_results: list[dict[str, Any]] = []
        stopped_reason: str | None = None
        source_observation: Observation | None = None
        executed_count = 0
        first_outcome: SingleActionOutcome | None = None
        # WP7b DELTA 1 (#3): the step's interference events are collected from EVERY
        # queue item — item 0 (the plan path's transition move) is only one
        # contributor; a guard event observed on the point's ACTION item must reach
        # the step reply and the lease like any other.
        step_events: list[str] = []
        if plan_context is not None:
            # The mutable prefix is internal orchestration state, never a
            # host-visible marker.  Limit/cancel exceptions can therefore be
            # answered from exactly the queue entries that were already resolved,
            # without re-running anything.
            plan_context["partial_results"] = follow_up_results
        for index, (item, item_effect) in enumerate(queue_items):
            self.stop_token.ensure_live()  # P0-C: kill path checked between queue items
            strict_digest = index > 0  # follow-ups stop on attached-window identity change
            # §1.4 delta 2 (MAJOR-1, ORVEX-2026-108): a limit trip on a QUEUED item is
            # caught HERE so the honest partial history survives. The triggering item
            # dispatched NOTHING (both raise sites are pre-dispatch: the observation
            # rate ceiling and ``check_action``), but items 0..N-1 DID dispatch input —
            # letting the exception escape would label the whole call "rejected, zero
            # effect" and drop their entries. Item 0 tripping still propagates (the
            # flat ``rejected`` reply, unchanged). Item N>=1 tripping converts to a
            # rejection entry below and stops the batch with the named reason.
            limit_trip: LimitExceeded | None = None
            try:
                outcome, post = await _run_item_with_partial_truth(
                    index,
                    item,
                    item_effect,
                    source_observation,
                    strict_digest=strict_digest,
                )
            except LimitExceeded as exc:
                if index == 0:
                    raise
                limit_trip = exc
                outcome = SingleActionOutcome(
                    kind="rejected",
                    interference_events=[],
                    reasons=[f"limit_exceeded: {exc.limit_name}"],
                    message=str(exc),
                )
                post = None
            if first_outcome is None:
                first_outcome = outcome  # the legacy response payload stays item 0's
            # WP7b DELTA 1 (#3): merge THIS item's guard events into the step's list
            # (every item, not only the first outcome's).
            step_events.extend(list(outcome.interference_events or []))
            # MINOR-N6 (VAL-WP1-02): the tripped item dispatched NOTHING, so it is not
            # counted as executed.
            if limit_trip is None:
                executed_count += 1
            follow_up_results.append(self._queue_entry(index, item, outcome))
            if limit_trip is not None:
                stopped_reason = "limit_exceeded"
                break
            # MINOR-N6: an ensure_app item whose TYPED outcome is a failure/ambiguous
            # (ok False — no target was attached and none can be established) stops the
            # batch: later speculative items must never run against a target that was
            # never established.
            ensure_outcome = outcome.result.ensure_app_outcome if outcome.result is not None else None
            if ensure_outcome in ("not_found", "launch_rejected", "launch_unresolved", "ambiguous"):
                stopped_reason = "ensure_app_failed"
                break
            # T8 named interference stops take precedence over the generic stop reasons:
            # a modal dialog or a post-keyboard focus drift halts the batch with a NAMED
            # reason the driver protocol teaches drivers to react to deliberately.
            interference_events = outcome.interference_events or []
            named_stop = next(
                (
                    reason
                    for reason in (
                        "modal_dialog" if any(e.startswith("MODAL_DIALOG") for e in interference_events) else None,
                        "focus_drifted" if any(e.startswith("FOCUS_DRIFTED") for e in interference_events) else None,
                    )
                    if reason is not None
                ),
                None,
            )
            if named_stop is not None:
                stopped_reason = named_stop
                break
            # REM-B (H2b queue-stop softening): an UNCERTAIN verification is not a
            # failure — the logged ctrl+a/hotkey keystrokes were flushed purely
            # because "no expectation stated" verdicts set ok=False. The queue now
            # CONTINUES past an item whose verification outcome is "uncertain" (no
            # expectation stated / a non-visual action the screenshot diff cannot
            # judge); every honest stop remains: non-executed kinds, DEFINITIVE
            # verification failures, safety rejections, approval requirements,
            # digest surprises, and the no-post-observation guard below. Per-item
            # results keep their own truthful verdict (ok=False + uncertain).
            if outcome.kind != "executed" or outcome.result is None:
                stopped_reason = (
                    self._interference_stop_reason(outcome)
                    or (outcome.kind if outcome.kind != "executed" else "verification_failed")
                )
                break
            item_verification = outcome.result.verification
            # an EXECUTED item whose verification outcome is
            # "failed" no longer DEFINITIVELY stops the queue. The input physically
            # dispatched and safety already admitted it; the next item re-grounds from
            # this item's fresh post-action capture, so nothing speculative ever runs
            # against an unseen screen. A FALSE failure (sub-threshold focus/caret
            # change, slow dialog open) must not flush the remaining batch items and
            # force 1-action-per-call driving. The honest failed verdict (ok=False,
            # note, evidence) still rides the per-item follow_up_results entry. Every
            # other stop is untouched: non-executed kinds (rejections, safety denial,
            # approval requirement, digest surprise, error), named interference stops,
            # the uncertain carve-out below, and the no-post-observation guard.
            # CORTEX_QUEUE_STRICT_VERIFY=1 restores the v0.5.5 stop-on-failed behavior
            # (read lazily; test-toggleable like CORTEX_DIFF_FAST).
            verification_failed = (
                not outcome.result.ok
                and item_verification is not None
                and item_verification.outcome == "failed"
            )
            if verification_failed and not _queue_strict_verify():
                pass  # continue: the honest failed verdict rides the per-item entry
            elif not outcome.result.ok and not (
                item_verification is not None and item_verification.outcome == "uncertain"
            ):
                stopped_reason = (
                    self._interference_stop_reason(outcome)
                    or (outcome.kind if outcome.kind != "executed" else "verification_failed")
                )
                break
            # ORVEX-2026-101 Batch 2 (RFC §2.3 T7/T8, WP7b): a PLAN point's last item
            # resolves the point. The point is failed when any of its items carried a
            # failed verification, uncertain when any carried uncertain (and none
            # failed); the point's own on_failure/on_uncertain policy then decides —
            # "stop" halts the sequence here with the named ``policy_stop`` reason,
            # "continue" proceeds with the honest verdict riding the entries.
            # Structural rejections never reach this point (they already stopped
            # above, regardless of any policy). The policy slice is STEP-LOCAL: the
            # plan step's context rewrote ``first_item_index`` to 0, so the slice
            # indexes this step's ``follow_up_results``.
            policy = (
                plan_policies[index]
                if plan_policies is not None and index < len(plan_policies)
                else None
            )
            if policy is not None and outcome.result is not None:
                point_entries = follow_up_results[policy["first_item_index"] : index + 1]
                point_failed = any(
                    entry.get("verification_outcome") == "failed" for entry in point_entries
                )
                point_uncertain = any(
                    entry.get("verification_outcome") == "uncertain" for entry in point_entries
                )
                if (point_failed and policy["on_failure"] == "stop") or (
                    point_uncertain
                    and not point_failed
                    and policy["on_uncertain"] == "stop"
                ):
                    stopped_reason = "policy_stop"
                    break
            if post is None:
                # Dry-run (or a degenerate executed outcome without a fresh capture):
                # nothing further can be verified — stop before speculating.
                stopped_reason = "no_post_action_observation"
                break
            source_observation = post
        assert first_outcome is not None  # the loop always runs at least once
        self._audit(
            "queue",
            action=action,
            result=stopped_reason or "completed",
            metadata={
                "items": len(queue_items),
                "executed": executed_count,
                "stopped_reason": stopped_reason or "",
            },
        )
        # Additive queue bookkeeping on the LEGACY first-item outcome: callers without
        # follow_ups see byte-identical shapes; queue callers get the extra fields.
        first_outcome.follow_up_results = follow_up_results
        first_outcome.follow_ups_stopped_reason = stopped_reason
        # WP7b DELTA 1 (#3): the merged per-item event list rides the first outcome
        # for the plan-step machinery; the flat reply's ``interference_events`` key
        # keeps publishing item 0's list (byte-identical flat contract).
        first_outcome.follow_up_interference_events = step_events
        return first_outcome

    @staticmethod
    def _interference_stop_reason(outcome: SingleActionOutcome) -> str | None:
        """Map a rejection's guard event payload to the NAMED queue stop reason (T8)."""
        if outcome.kind != "rejected":
            return None
        prefixes = {
            "FOCUS_TAKEN_BY": "focus_taken_by",
            "FOCUS_IDENTITY_UNKNOWN": "focus_identity_unknown",
            "FOCUS_DRIFTED": "focus_drifted",
            "MODAL_DIALOG": "modal_dialog",
            "STUCK_MODIFIER": "stuck_modifier",
            "TARGET_GONE": "target_gone",
        }
        for reason in outcome.reasons:
            first = str(reason).split(" ", 1)[0]
            if first in prefixes:
                return prefixes[first]
        return None

    @staticmethod
    def _queue_entry(index: int, action: GroundedAction, outcome: SingleActionOutcome) -> dict[str, Any]:
        """Slim bounded per-item queue result (redaction at the sink).

        REM-A H5/H6 (master-mission Phase 2): queue entries carry ONLY the per-item
        essentials (ok, action identity, verification outcome/note, confidences) —
        never screenshot bytes in ANY form (nested or otherwise) and never a second
        copy of the top-level payload blobs: the primary action's full result rides
        the top-level response payload exactly once.
        """
        result = outcome.result
        entry: dict[str, Any] = {
            "index": index,
            "action_id": action.action_id,
            "action_type": action.action.value,
            "kind": outcome.kind,
            # §1 (ORVEX-2026-108): identical envelope on every follow_up_results[i] —
            # the token comes from the SAME derivation as the flat reply, the reasons
            # (rejection kinds ONLY, see below) from the SAME shared helper with the
            # typed code as first reason, and ``ok`` from the SAME shared ok-rule
            # (ok is True iff result ∈ {verified, marker}).
            "message": outcome.message or (result.message if result is not None else ""),
            "requires_approval": outcome.requires_approval,
            "model_confidence": outcome.model_confidence,
            "grounding_confidence": outcome.grounding_confidence,
            "verification_confidence": outcome.verification_confidence,
        }
        entry["result"] = execute_result_token(outcome)
        entry["ok"] = ok_from_result(entry["result"])
        # §1.1 (identical field set as flat): the typed ensure_app outcome rides the
        # entry exactly as it rides the flat reply (None on every other action).
        entry["ensure_app_outcome"] = (
            result.ensure_app_outcome if result is not None else None
        )
        # WP2 (ORVEX-2026-101 Batch 2, additive, present only when set — entries for
        # calls without hold/duration are byte-identical): a queued held/timed input
        # reports the per-item durations so the caller's evidence names them.
        if action.hold_ms is not None:
            entry["hold_ms"] = action.hold_ms
        if action.duration_ms is not None:
            entry["duration_ms"] = action.duration_ms
        repositions = list(getattr(action, "_repositions", []) or [])
        if repositions:
            entry["repositions"] = repositions
        # MINOR-N1 (VAL-WP1-02): reasons exist ONLY on rejection kinds — every other
        # entry has NO reasons key, exactly like the flat reply of the same kind.
        if outcome.kind in ("rejected", "safety_denied", "digest_surprise"):
            entry["reasons"] = rejection_reasons(outcome)
        if outcome.partial_delivery is not None:
            entry["partial_delivery"] = dict(outcome.partial_delivery)
        if result is not None and result.verification is not None:
            # §1 (ORVEX-2026-108): every follow_up_results[i] carries the SAME
            # envelope fields as the flat reply — including a compact verification
            # block (outcome/confidence/evidence/method; never any image bytes, so
            # REM-A H5 slimness holds).
            verification = result.verification
            entry["verification"] = {
                "outcome": verification.outcome,
                "confidence": float(verification.confidence),
                "evidence": [str(item)[:200] for item in verification.evidence[:3]],
                "verification_method": verification.verification_method,
            }
            entry["verification_outcome"] = verification.outcome
            entry["verification_note"] = verification.note[:200]
        return entry

    async def _run_single_pipeline(
        self,
        state: Any,
        action: GroundedAction,
        *,
        approved: bool,
        expected_effect: str | None = None,
        source_observation: Observation | None = None,
        strict_digest: bool = False,
    ) -> tuple[SingleActionOutcome, Observation | None]:
        """One direct action through the full pipeline; returns (outcome, post-capture).

        ``source_observation`` (PERF-004 C1 observe reuse) injects the caller's fresh
        post-action capture as the grounding source; ``None`` captures at
        ``direct_request`` (rate-gated, host-driven). ``strict_digest`` (queued
        follow-ups) applies per-item premise validity before executing the
        speculative item: the attached window's identity (hwnd + title + bounds)
        changed or closed since the premise -> a ``digest_surprise`` stop
        (``CORTEX_QUEUE_STRICT_DIGEST=1`` restores the v0.5.6 whole-screen digest
        comparison; ordinary pixel changes from earlier items no longer stop).

        The returned observation is the fresh post-action capture (``None`` when no
        execution happened — dry-run, rejection, stop) so the caller can reuse it.
        """
        try:
            # §1.2 marker kinds (ORVEX-2026-108): ``done``/``wait`` make no screen
            # claim, so a DIRECT marker has no premise to capture — its ONE capture is
            # taken in the rate-exempt ``post_action`` phase and serves as the
            # pipeline's observation, the post-action observation and the response
            # image at once. A QUEUED marker reuses the previous item's post-action
            # capture as its premise (no capture) and still takes its own fresh
            # post-action frame, so the premise it hands to the NEXT queue item is as
            # fresh as any other action's — the digest/identity premise check is not
            # weakened.
            marker = action.action in _NO_OP_MARKER_KINDS
            if source_observation is not None:
                observation = source_observation
                self.metrics.incr("observation_reuse")
                self.task.record_observation_id(observation.observation_id)
                self._audit(
                    "observation",
                    observation=observation,
                    result="ok",
                    duration_ms=0.0,
                    phase="direct_request",
                    reused=True,
                )
            elif marker:
                observation = self._observe("post_action")
            else:
                observation = self._observe("direct_request")
            try:
                grounding = self._ground(action, observation)
            except Exception as exc:  # noqa: BLE001 - grounding refusal is a rejection
                self.metrics.incr("grounding_failure")
                self._audit(
                    "grounding",
                    observation=observation,
                    action=action,
                    result="failed",
                    metadata={"exception": type(exc).__name__, "detail": str(exc)[:200]},
                )
                return (
                    SingleActionOutcome(
                        kind="rejected",
                        reasons=[f"Grounding failed: {exc}"],
                        # name the REAL gate in `message` — weak drivers
                        # read `message` first and a generic "Grounding rejected."
                        # sent them decoding `reasons` for turns.
                        message=f"Action rejected by grounding: {str(exc)[:180]}",
                    ),
                    None,
                )

            # T8 mechanism (i): arm the focus binding from an allowlisted observation.
            self._bind_guard_from_observation(observation)
            # R-5 (W1) validate-phase capture sharing: a DIRECT action's premise was
            # captured at the start of THIS tool call (nothing but pure computation has
            # run since — grounding + guard binding, zero input dispatched), so when it
            # is still inside the freshness window the premise IS the current screen and
            # re-capturing the identical frame is pure latency. The sharing is GUARDED
            # by a cheap identity probe (monitors + foreground window + coordinate
            # space — the exact dimensions the validator's staleness check consumes; no
            # pixels): no drift means the premise still describes the screen and is
            # reused (audited truthfully: phase="validate", reused=True, duration 0.0);
            # ANY drift, a stale premise, a queued follow-up (whose premise is a
            # PREVIOUS item's post-action capture), or a backend without the probe
            # falls back to the fresh validate capture below exactly as before.
            premise_captured_at = getattr(observation, "_captured_monotonic", 0.0)
            reuse_window_ms = _validate_reuse_window_ms()
            current: Observation | None = None
            probe_used = False
            if (
                not marker
                and source_observation is None
                and not strict_digest
                and reuse_window_ms > 0.0
                and premise_captured_at > 0.0
                and (time.perf_counter() - premise_captured_at) * 1000.0 <= reuse_window_ms
            ):
                probe = self.backend.identity_probe(observation)
                if probe is not None:
                    probe_used = True
                    drift = self.validator._staleness_drift(observation, probe)
                    if drift is None:
                        current = observation
                        self.metrics.incr("observation_reuse")
                        self._audit(
                            "observation",
                            observation=current,
                            result="ok",
                            duration_ms=0.0,
                            phase="validate",
                            reused=True,
                        )
                    else:
                        # Real drift (window/process/monitor/space): feed the probe to
                        # the validator as the current observation so the P0-H
                        # STALE_OBSERVATION rejection + single re-observe recovery run
                        # with fresh identity — never execute against a drifted premise.
                        current = probe
                        self.metrics.incr("identity_probe_drift")
                        self._audit(
                            "observation",
                            observation=current,
                            result="ok",
                            duration_ms=0.0,
                            phase="validate",
                            reused=True,
                            probe=True,
                            drift=drift,
                        )
            if current is None:
                # F2-4: a marker's "current screen" IS its own (fresh) observation —
                # there is nothing to staleness-check against pixels, so the second
                # capture would be pure cost. ACCEPTED DEV PARITY (VAL-WP1 MINOR,
                # Commander decision; dev agent.py:2074): a marker therefore skips the
                # validate capture and the queue's premise check — a batched marker
                # absorbs a window change the old pipeline would have stopped.
                current = observation if marker else self._observe("validate")
            if strict_digest and not _queue_premise_valid(observation, current):
                # PERF-004 C7: the queued action's premise is no longer
                # current. Per-item validity: the attached window's identity (hwnd +
                # title + bounds) changed or closed since the premise — TRUE
                # staleness; speculative actions never run against a window nobody is
                # driving. A mere pixel change (earlier items drew/typed) does NOT
                # stop the batch anymore; ``CORTEX_QUEUE_STRICT_DIGEST=1`` restores
                # the legacy whole-screen digest stop.
                premise_id = _queue_window_identity(observation)
                current_id = _queue_window_identity(current)
                if (
                    premise_id is not None
                    and current_id is not None
                    and premise_id != current_id
                ):
                    detail = "the attached window's identity changed"
                elif premise_id is not None and current_id is None:
                    detail = "the attached window closed"
                elif premise_id is None and current_id is not None:
                    detail = "an attached window identity appeared where none was"
                else:
                    detail = "the screen changed since the premise (identity-less backend)"
                self.metrics.incr("digest_surprise")
                self._audit(
                    "validation",
                    observation=current,
                    action=action,
                    result="digest_surprise",
                    metadata={
                        "reason": (
                            f"Post-action staleness: {detail} since the queued "
                            "action's premise was captured; the queue stopped."
                        ),
                        "premise_window": premise_id,
                        "current_window": current_id,
                    },
                )
                return (
                    SingleActionOutcome(
                        kind="digest_surprise",
                        message=(
                            f"Post-action staleness: {detail} since this queued "
                            "action's premise was captured; the queue stopped "
                            "before executing it."
                        ),
                    ),
                    None,
                )
            validation = self.validator.validate(
                action,
                observation,
                state,
                current_observation=current,
                allowed_processes=self.allowed_processes or None,
            )
            if not validation.valid and "STALE_OBSERVATION" in validation.codes:
                # P0-H: automatic single re-observe + re-validate on staleness.
                # (Skipped for strict queued items: a drifted premise stops the queue
                # as a digest surprise before this branch can run.)
                try:
                    fresh = self._observe("revalidate")
                    self._ground(action, fresh)
                except Exception as re_ground_error:  # noqa: BLE001
                    self.metrics.incr("grounding_failure")
                    return (
                        SingleActionOutcome(
                            kind="rejected",
                            reasons=[f"Stale observation and re-grounding failed: {re_ground_error}"],
                            # name the staleness gate, not a generic stamp.
                            message=f"Action rejected by staleness check: {str(re_ground_error)[:180]}",
                        ),
                        None,
                    )
                observation = fresh  # the freshest pre-action observation is the baseline
                validation = self.validator.validate(
                    action,
                    fresh,
                    state,
                    current_observation=fresh,
                    allowed_processes=self.allowed_processes or None,
                )
            self._audit(
                "validation",
                observation=observation,
                action=action,
                result="ok" if validation.valid else "rejected",
                metadata={
                    "reasons": validation.reasons[:3],
                    "codes": list(validation.codes)[:5],
                    "staleness_proof": (
                        "digest_match" if digest_matches(observation, current) else "digest_mismatch"
                    ),
                },
            )
            if not validation.valid:
                self.metrics.incr("grounding_failure")
                return (
                    SingleActionOutcome(
                        kind="rejected",
                        reasons=list(validation.reasons),
                        # name the REAL gate in `message` — the live
                        # session's allowlist rejection arrived as a generic
                        # "Grounding rejected." and cost the driver thinking turns.
                        message=(
                            "Action rejected by validation: "
                            + (str(validation.reasons[0])[:180] if validation.reasons else "grounding failed.")
                        ),
                    ),
                    None,
                )

            focus_rejection = self._focus_allowlist_rejection(action, state)
            if focus_rejection is not None:
                # focus_window allowlist gate (before safety): same rejection shape and
                # WRONG_WINDOW recovery mapping as ordinary allowlist violations.
                self.metrics.incr("grounding_failure")
                self._audit(
                    "validation",
                    observation=observation,
                    action=action,
                    result="rejected",
                    metadata={
                        "reasons": focus_rejection.reasons[:3],
                        "codes": list(focus_rejection.codes)[:5],
                    },
                )
                return (
                    SingleActionOutcome(
                        kind="rejected",
                        reasons=list(focus_rejection.reasons),
                        # the focus-window allowlist gate names itself.
                        message=(
                            "Action rejected by focus allowlist: "
                            + (
                                str(focus_rejection.reasons[0])[:180]
                                if focus_rejection.reasons
                                else "focus gate refused."
                            )
                        ),
                    ),
                    None,
                )

            safety_decision = self._evaluate_safety(action, state, observation)
            self._audit(
                "safety",
                observation=observation,
                action=action,
                result="allowed" if safety_decision.allowed else "denied",
                metadata={
                    "reason": str(getattr(safety_decision, "reason", ""))[:200],
                    "risk": str(getattr(safety_decision, "risk", "") or ""),
                },
            )
            if not safety_decision.allowed:
                self.metrics.incr("safety_block")
                return (
                    SingleActionOutcome(kind="safety_denied", message=str(safety_decision.reason)),
                    None,
                )
            if safety_decision.requires_approval and not approved:
                self.metrics.incr("approval_requested")
                self.metrics.incr("approval_denied")
                self._audit(
                    "approval",
                    observation=observation,
                    action=action,
                    result="required",
                    metadata={"reason": str(safety_decision.reason)[:200]},
                )
                return (
                    SingleActionOutcome(
                        kind="approval_required",
                        message=str(safety_decision.reason),
                        requires_approval=True,
                    ),
                    None,
                )

            if getattr(state, "dry_run", False):
                # §1.2/§1.4.3 (ORVEX-2026-108; dev F2-5 decision D-7a): a dry run is
                # not an execution, on EVERY field that says so. ``ok=True`` next to a
                # phantom ``failed`` verification asserted both "it worked" and "it
                # failed" in one payload; ``ok=False`` plus the ``dry_run`` marker and
                # the top-level ``result:"dry_run"`` token (assigned by the reply
                # builder through :func:`execute_result_token`) make the row
                # unambiguous, and the verification is built through the explicit
                # ``outcome`` form so it reads ``uncertain`` ("cannot determine")
                # instead of the phantom ``failed`` a never-dispatched action has not
                # earned, with ``verification_method:"dry_run"`` naming the tier.
                # This branch is still BEFORE ``_backend_execute`` and before the
                # post-action capture, so a dry run dispatches no input and performs
                # no post-action capture.
                return (
                    SingleActionOutcome(
                        kind="executed",
                        result=ExecutionResult(
                            ok=False,
                            action=action,
                            message=(
                                "DRY-RUN (no input dispatched): action validated "
                                "but not executed."
                            ),
                            verification=VerificationResult(
                                outcome="uncertain",
                                changed=False,
                                note=(
                                    "DRY-RUN (no input dispatched): execution and "
                                    "post-action verification were not performed."
                                ),
                                confidence=1.0,
                                evidence=[
                                    "dry_run: the action was validated and then "
                                    "deliberately not dispatched"
                                ],
                                verification_method="dry_run",
                            ),
                            dry_run=True,
                        ),
                        model_confidence=action.confidence,
                        grounding_confidence=grounding.confidence,
                    ),
                    None,
                )

            # --- T8 Interference Guard (pre-dispatch, after the dry-run check) --------
            # Protection upgrade only: a blocking verdict is a structured REJECTION —
            # the foreign window is never acted on. observe_only/warn annotate instead.
            guard_verdict = self._guard_pre_dispatch(action)
            if guard_verdict is not None and guard_verdict.blocking:
                return self._guard_rejection_outcome(guard_verdict), None
            pre_annotation = guard_verdict.message if guard_verdict is not None else None

            self.enforcer.check_action()
            self.enforcer.begin_action()
            self.stop_token.ensure_live()
            started = time.perf_counter()
            try:
                message = self._backend_execute(action, self.stop_token)
            except FocusDriftError as exc:
                # T8 mechanism iv: mid-type focus drift — abort cleanly (no same-instance
                # retry: a retype would duplicate the delivered prefix).
                self.metrics.incr("action_total")
                self.metrics.incr("action_failure")
                self.metrics.incr("interference_rejections")
                self._audit(
                    "interference",
                    observation=observation,
                    action=action,
                    result="focus_drifted_mid_type",
                    metadata={
                        "payload": str(exc)[:400],
                        "delivered_chars": getattr(exc, "delivered_chars", None),
                    },
                )
                # §1.1 K-05 (ORVEX-2026-108): a rejection is zero-effect EXCEPT here —
                # when the guard aborted after at least one chunk landed, the reply
                # carries ``partial_delivery.delivered_chars`` so the driver knows the
                # field holds a partial prefix. Before any chunk -> no key at all
                # (absence means "nothing dispatched").
                delivered = getattr(exc, "delivered_chars", None)
                partial = (
                    {"delivered_chars": int(delivered)}
                    if isinstance(delivered, int) and not isinstance(delivered, bool) and delivered > 0
                    else None
                )
                return (
                    SingleActionOutcome(
                        kind="rejected",
                        reasons=[str(exc), REFOCUS_HINT],
                        message=(
                            "Focus continuity: focus drifted mid-type; the in-flight type "
                            "was aborted before further chunks could land in a foreign field."
                        ),
                        partial_delivery=partial,
                    ),
                    None,
                )
            duration_ms = (time.perf_counter() - started) * 1000.0
            self.metrics.record_latency("execution_ms", duration_ms)
            self.enforcer.record_action()
            self.metrics.incr("action_total")
            self.metrics.incr("action_success")
            state.step_count += 1
            self.task.step_count += 1
            self.task.record_action(action)
            self._audit(
                "execution",
                observation=observation,
                action=action,
                result="ok",
                duration_ms=duration_ms,
                metadata={
                    "message": str(message)[:200],
                    # WP2 (Batch 5 decision 2): the timed-motion interference evidence
                    # rides the audit event too (additive; empty list = none).
                    "repositions": list(getattr(action, "_repositions", []) or []),
                },
            )

            # B1 GUARANTEE (T8): verification ALWAYS captures its own fresh post-action
            # observation here, internally — it can never depend on the screenshot the
            # RESPONSE was configured to omit (``include_screenshot_after=false`` pops
            # the image from the response payload only, after verification has run).
            # §1.2 markers (ORVEX-2026-108): a DIRECT marker kind had no premise to
            # capture, so its ONE capture was already taken in the post_action phase
            # above and is reused here — a marker makes no screen claim, so there is
            # no second frame to take.
            after = (
                observation
                if (marker and source_observation is None)
                else self._observe("post_action")
            )
            # T8: re-bind after an explicit focus/reattach action; run the sentinel.
            if action.action in {ActionType.FOCUS_WINDOW, ActionType.ENSURE_APP}:
                self.guard.rebind_from_observation(after)
            if action.action in {ActionType.KEYPRESS, ActionType.HOTKEY}:
                self.guard.note_chord(action.keys)
            post_events, post_annotations, post_stops = self._post_action_guard_events(action, after)
            outcome_message = str(message)
            intent = self._build_intent(action, None, expected_effect)
            verify_started = time.perf_counter()
            ladder_tier = "engine"
            capture_marker: str | None = None
            if marker:
                # §1.2 (ORVEX-2026-108; dev F2-4): a MARKER is verified by
                # construction, and that is honest: it claimed no screen transition,
                # so there is nothing to fail to prove. The pixel ladder is skipped
                # entirely (it could only ever abstain on a screen nobody touched),
                # and a stated ``expected_effect`` is NOT consulted — a marker cannot
                # make an effect happen, so claiming to have checked would be the very
                # lie this contract exists to remove. The confidence is about the
                # MARKER being handled, not about the task; the reply's top-level
                # ``result`` token is ``"marker"`` (assigned by the reply builder).
                ladder_tier = "no_op_marker"
                verification = VerificationResult(
                    outcome="verified",
                    changed=False,
                    note=(
                        f"{action.action.value} is a completion marker: it makes no "
                        "screen claim, so there is no transition to verify and no "
                        "screenshot-diff tier ran. The marker was dispatched and "
                        "handled; the task's stated effect is NOT asserted here."
                    ),
                    confidence=_NO_OP_MARKER_CONFIDENCE,
                    evidence=[f"no_op_marker: {action.action.value} dispatched"],
                    verification_method="no_op_marker",
                    observation_id=after.observation_id,
                )
            elif action.action is ActionType.ENSURE_APP and self._ensure_app_probe_outcome(outcome_message):
                # §1.3 (ORVEX-2026-108, MAJOR-4): the probe outcome is the answer the
                # driver acts on — and its VALUE decides the verdict, never a blanket
                # "verified". reattached/launched prove the attach/spawn (verified);
                # ambiguous means the tool did NOT establish which window (uncertain,
                # ok False — uncertain is never success); not_found / launch_rejected /
                # launch_unresolved are launch FAILURES (failed, ok False) — shipping
                # them with ok:true was a label lie even before WP4b.
                ladder_tier = "ensure_app_probe"
                probe_outcome = ensure_app_outcome(outcome_message)
                if probe_outcome in ("not_found", "launch_rejected", "launch_unresolved"):
                    verification = VerificationResult(
                        outcome="failed",
                        changed=False,
                        note=(
                            f"ensure_app {probe_outcome}: the probe ran and reported a "
                            "launch FAILURE, so its outcome is not a success; re-observe "
                            "and decide (the target was not attached)."
                        ),
                        confidence=_ENSURE_APP_PROBE_FAILURE_CONFIDENCE,
                        evidence=[outcome_message[:400]],
                        verification_method="ensure_app_probe",
                        observation_id=after.observation_id,
                    )
                elif probe_outcome == "ambiguous":
                    verification = VerificationResult(
                        outcome="uncertain",
                        changed=False,
                        note=(
                            "ensure_app ambiguous: several instances matched; the tool "
                            "did not establish WHICH window is the target — re-observe "
                            "and disambiguate with a doc-token."
                        ),
                        confidence=_ENSURE_APP_PROBE_FAILURE_CONFIDENCE,
                        evidence=[outcome_message[:400]],
                        verification_method="ensure_app_probe",
                        observation_id=after.observation_id,
                    )
                else:
                    # reattached / launched (or an unrecognised shape that still
                    # carries a probe prefix): the probe ran and proves the outcome.
                    verification = VerificationResult(
                        outcome="verified",
                        changed=False,
                        note=f"ensure_app probe outcome (no screen claim): {outcome_message[:200]}",
                        confidence=0.9,
                        evidence=[outcome_message[:400]],
                        verification_method="ensure_app_probe",
                        observation_id=after.observation_id,
                    )
            else:
                # F2-3: the ladder may run more than once for ONE action (the single
                # late-repaint re-capture below, the expected-text fallback), so the
                # ladder runs unrecorded and the FINAL verdict is audited exactly once
                # by ``_record_verification`` after the demotions below (E-05).
                ladder_tier = "engine"
                # F1 (VAL-WP4a): the primary ladder run returns its REAL tier so the
                # single audit record names it (v0.7.5 behaviour), exactly like the
                # recapture and fallback branches below.
                verification, ladder_tier = await self._verify(
                    intent, observation, after, action=action, return_tier=True
                )
                # F2-3 + E-02: an ``uncertain`` that is SPECIFICALLY "the two frames
                # were byte-identical" earns ONE late-repaint re-capture — but only
                # when a fresh frame could plausibly carry NEW evidence (E-02
                # conditioning): a stated ``expected_effect`` (the verdict is about a
                # described transition a late repaint can still prove) or a
                # repaint-prone action class (type/scroll). An effect-less
                # click/move/keypress on a static screen performs ZERO extra captures
                # (captures/action stays at the WP0 baseline). Bounded to exactly ONE
                # re-capture (``_IDENTICAL_FRAME_RECHECK_LIMIT``); the capture phase is
                # the rate-exempt ``post_action``. A NON-identical uncertain is
                # deliberately left alone: re-capturing there would let noise upgrade a
                # verdict, which the doctrine forbids.
                if (
                    _is_identical_frame_uncertain(verification)
                    and (
                        # F6 (VAL-WP4a): read the INTENT's effect — ``_build_intent``
                        # merges the parameter AND ``action.expected_effect``, so an
                        # effect riding only on the action conditions the recapture too.
                        bool(intent.expected_effect)
                        or action.action in _REPAINT_PRONE_ACTIONS
                    )
                ):
                    if self.stop_token.wait(_IDENTICAL_FRAME_RECHECK_SETTLE_SECONDS):
                        self.stop_token.ensure_live()
                    after = self._observe("post_action")
                    self.metrics.incr("verification_recapture")
                    verification, ladder_tier = await self._verify(
                        intent, observation, after, action=action, return_tier=True
                    )
                # T8 B2: an uncertain expected-text verdict (typed text invisible to
                # window/UI-control evidence) routes to the reliable pixel-diff tier.
                if (
                    verification.outcome == "uncertain"
                    and intent.kind == VerificationKind.EXPECTED_TEXT.value
                ):
                    # Legacy-compat degradation for direct client calls: fall back to the
                    # deterministic visual-change check; the final outcome still comes
                    # from evidence; uncertain is never upgraded.
                    fallback_intent = self._build_intent(action, "visual_change", expected_effect)
                    verification, ladder_tier = await self._verify(
                        fallback_intent, observation, after, action=action, return_tier=True
                    )
                if action.action is ActionType.TYPE:
                    # F2-1: the focused control's R-04 read-back outranks a title/control
                    # substring match — an ``integrity=mismatch`` vetoed verified to the
                    # honest §1 token (uncertain, ok False), never ok:true.
                    verification = _apply_type_integrity_status(
                        verification, outcome_message, changed=verification.changed
                    )
                _, capture_marker = self._capture_source_guard(action, verification)
            if pre_annotation:
                post_annotations.insert(0, pre_annotation)
            if post_annotations:
                verification = verification.model_copy(
                    update={"note": f"{verification.note} | {' | '.join(post_annotations)}"[:900]}
                )
            drift_post = any(e.startswith("FOCUS_DRIFTED") for e in post_events)
            if drift_post and verification.outcome == "verified":
                # T8: only a post-keyboard FOCUS_DRIFTED abort is an honest not-ok;
                # MODAL_DIALOG annotates on single actions (A12 single-action semantics).
                verification = VerificationResult(
                    outcome="failed",
                    changed=verification.changed,
                    note=f"Post-action interference: {' | '.join(post_stops + post_events)}"[:900],
                    confidence=verification.confidence,
                    evidence=list(post_events),
                    verification_method="interference_post_action",
                    observation_id=after.observation_id,
                )
            # E-05 (ORVEX-2026-106, fixed during the WP4b port): the ONE verification
            # audit event is emitted AFTER every demotion — the type-integrity veto
            # above, the launch-outcome demotion in the probe branch, and THIS drift
            # demotion (verified -> failed; the last audit/reply disagreement left by
            # VAL-WP4a F2) — so the audit verdict always equals the reply verdict. All
            # three branches (marker, probe, ladder) funnel through the same single
            # record: done/wait markers and ensure_app probes now emit the event too
            # (the probe branch previously emitted NO verification audit event at all).
            self._record_verification(
                verification,
                intent,
                after,
                action,
                ladder_tier=ladder_tier,
                duration_ms=(
                    (time.perf_counter() - verify_started) * 1000.0
                    if ladder_tier not in ("no_op_marker", "ensure_app_probe")
                    else 0.0
                ),
                capture_marker=capture_marker,
            )
            ok = verification.outcome == "verified"
            if ok:
                self._verified_reanchor(self.guard, verification.outcome, after)
            outcome = SingleActionOutcome(
                kind="executed",
                result=ExecutionResult(
                    ok=ok,
                    action=action,
                    message=str(message),
                    verification=verification,
                    screenshot_after_base64=after.image_base64,
                    # §1.1 S-12 (ORVEX-2026-108): the typed outcome, derived where the
                    # probe shortcut already inspects the message. ``None`` on every
                    # other action — additive, and the §1.1 ok-rule is unchanged.
                    ensure_app_outcome=(
                        ensure_app_outcome(str(message))
                        if action.action is ActionType.ENSURE_APP
                        else None
                    ),
                ),
                model_confidence=action.confidence,
                grounding_confidence=grounding.confidence,
                verification_confidence=verification.confidence,
                interference_events=post_events or None,
            )
            try:  # R-5 (W4): the post-action frame rides along for outbound bounding
                outcome.result._frame = getattr(after, "_frame", None)
            except Exception:  # noqa: BLE001 - never break the executed path
                pass
            return outcome, after
        except TaskStopped:
            raise
        except LimitExceeded as exc:
            self._audit("limit_exceeded", result="failed", metadata={"limit": exc.limit_name})
            raise
        except Exception as exc:  # noqa: BLE001 - fail-closed: structured error, no crash
            self._audit(
                "failure",
                result="error",
                metadata={"exception": type(exc).__name__, "detail": str(exc)[:300]},
            )
            return SingleActionOutcome(kind="error", message=f"{type(exc).__name__}: {exc}"), None
