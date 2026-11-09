"""The MCP boundary: REM-F tolerant coercions, REM-G plain advertised schemas, and the teaching vocabulary shared by the tool modules."""

from __future__ import annotations

import json
import math
from typing import Annotated, Any

from pydantic import BeforeValidator

from ..planning import plan as plan_layer



# --- REM-F weak-model tolerant argument coercion (ORVEX-CORTEX-055, boundary only) --------------
# The live Kimi Code / GLM-5V run proved the pipeline works but the DRIVING MODEL
# could not pass the strict pydantic schema four times (float coordinates, a
# follow_ups array shape, an allowed_processes array) and gave up on clicking.
# These helpers add BOUNDARY tolerance on exactly that class — the coercion runs
# PRE-validation, and every downstream internal model (GroundedAction, Limits,
# ActionSpec bounds) stays strict and unchanged. Non-numeric garbage still fails
# with a clean pydantic-style typed error — this is tolerance, not semantics
# change. D5 (ORVEX-CORTEX-056-LIVEFIX): the ADVERTISED schemas for the four
# live-friction classes (x/y/x2/y2, allowed_processes/allowed_windows,
# follow_ups, keys) are now WIDENED flat type-arrays that also admit the string
# (and, for follow_ups, object) shapes the client-side validator used to reject
# before these coercions could ever run; everything else keeps the plain
# "integer"/"array" advertisement.


#: Names of the action-spec fields that carry integer coordinates (used to coerce
#: follow_ups entries with the same policy as the tool parameters).
_SPEC_COORDINATE_FIELDS = ("x", "y", "x2", "y2")


def _coerce_tolerant_int(value: Any, *, field: str = "value") -> int:
    """Boundary coercion for one integer tool argument (weak-model tolerance).

    Accepts ints as-is; integral floats (1343.0) and numeric strings ("1343") pass
    through; NON-INTEGRAL floats/strings ROUND to the nearest int — a vision model
    aiming at pixel 1343.7 must not hard-fail at the schema boundary. Booleans,
    NaN/infinity, non-numeric strings, and non-number types raise a clean
    pydantic-style ValueError instead of crashing.
    """
    if isinstance(value, bool):
        # ValueError (not TypeError): pydantic only converts ValueError/AssertionError
        # inside BeforeValidator into a typed ValidationError — a raw TypeError would
        # escape the validation boundary as a crash (verified against pydantic 2.13).
        raise ValueError(  # noqa: TRY004 - deliberate: see the comment above
            f"{field}: boolean is not a valid integer coordinate."
        )
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{field}: non-finite number is not a valid integer coordinate.")
        return round(value)
    if isinstance(value, str):
        text = value.strip()
        try:
            number = float(text)
        except ValueError:
            raise ValueError(
                f"{field}: cannot interpret {value!r} as an integer coordinate."
            ) from None
        if not math.isfinite(number):
            raise ValueError(f"{field}: non-finite number is not a valid integer coordinate.")
        return round(number)
    raise ValueError(
        f"{field}: cannot interpret a {type(value).__name__} as an integer coordinate."
    )


#: ``x``/``y``/``x2``/``y2`` tool type — schema still says "integer" (Annotated
#: int), coercion runs before validation. Covers int, integral float, fractional
#: float (rounded), and numeric string inputs.
TolerantInt = Annotated[int, BeforeValidator(_coerce_tolerant_int)]


def _reject_bool_duration(value: Any) -> Any:
    """DELTA 3 NIT boundary: pydantic's LAX float would coerce ``True`` to 1.0
    (verified live through the instance gate). This boundary validator rejects
    the boolean BEFORE that coercion — over MCP it surfaces as a pydantic
    isError whose message names ``duration_s`` (NOT the typed envelope); a
    direct (non-MCP) caller bypasses this boundary and reaches
    computer_record's own typed ``invalid_record_duration`` rejection instead
    (server/record.py)."""
    if isinstance(value, bool):
        raise ValueError(
            f"duration_s must be a number of seconds; got {value!r} "
            "(a boolean is not a duration)."
        )
    return value


#: ``computer_record``'s ``duration_s`` — lax float for every number and
#: numeric string (the parity the record object matches), booleans rejected
#: at the boundary.
RecordDuration = Annotated[float, BeforeValidator(_reject_bool_duration)]


def _coerce_tolerant_str_list(value: Any) -> list[str] | None:
    """Boundary coercion for allowlist parameters (``allowed_processes``/windows).

    Accepts list[str] as today, PLUS weak-model serializations: a comma- or
    space-separated STRING ("mspaint.exe, notepad.exe" -> two entries) and a single
    bare string ("mspaint.exe" -> one entry). Empty/whitespace-only string yields
    [] (same as an omitted value). Anything else raises a typed error.
    """
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return []
        return [item for item in stripped.replace(",", " ").split() if item]
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    # ValueError (not TypeError): pydantic converts only ValueError/AssertionError
    # inside BeforeValidator into a typed ValidationError — a raw TypeError would
    # escape the validation boundary as a crash.
    raise ValueError(
        f"must be a list of strings (or a comma/space-separated string), "
        f"got {type(value).__name__}."
    )


#: Allowlist parameter type — schema still says array-of-string.
TolerantStrList = Annotated[list[str], BeforeValidator(_coerce_tolerant_str_list)]


def _coerce_tolerant_follow_ups(value: Any) -> list[dict[str, Any]] | None:
    """Boundary coercion for the ``follow_ups`` queue (weak-model tolerance).

    Accepts list[dict] as today, PLUS: a JSON-encoded STRING containing the list
    (parsed), a single dict (wrapped into [dict]), and per-entry JSON strings (each
    parsed). Entries keep ``dict[str, Any]`` (unknown keys already tolerated);
    non-JSON strings and non-object entries raise a typed error — the queue is
    never silently truncated or guessed at.
    """
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"follow_ups string is not valid JSON: {exc}") from None
    if isinstance(value, dict):
        value = [value]
    if isinstance(value, (list, tuple)):
        coerced: list[dict[str, Any]] = []
        for item in value:
            if isinstance(item, str):
                try:
                    item = json.loads(item)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"follow_ups entry is not valid JSON: {exc}") from None
            if not isinstance(item, dict):
                # ValueError (not TypeError): pydantic converts only ValueError/
                # AssertionError inside BeforeValidator into a typed ValidationError.
                raise ValueError(  # noqa: TRY004 - deliberate, see comment above
                    "each follow_ups entry must be an action-spec object."
                ) from None
            coerced.append(item)
        return coerced
    raise ValueError("follow_ups must be a list of action-spec objects.") from None


#: Follow-up queue type — schema still says array-of-object (dict[str, Any]).
TolerantFollowUps = Annotated[list[dict[str, Any]], BeforeValidator(_coerce_tolerant_follow_ups)]


# --- REM-G plain advertised schemas (ORVEX-CORTEX-055, live-test hardening 2) --------------------
# TWO further Kimi Code / GLM-5V live runs (REM-F runtime coercion already in
# place) still failed on EVERY parameter whose ADVERTISED wire schema uses
# pydantic's Optional anyOf union form — a plain integer x=679, a plain array
# keys=["enter"], a valid follow_ups array, allowed_processes=["mspaint.exe"]
# all came back "/x must be integer; /x must be null; /x must match a schema in
# anyOf" from the CLIENT-side validator, while plain-string params passed. The
# client chokes on the anyOf UNION FORM itself, not the types.
#
# Fix (advertised shape only — runtime validation is EXACTLY as today): a small
# ``_PlainJsonSchema`` marker attached via ``Annotated[T | None, marker]``
# flattens each Optional parameter's advertised JSON Schema from anyOf[X, null]
# to the maximally-compatible type-array form ({"type": ["integer", "null"]},
# {"type": ["array", "null"], "items": ...}). The marker is the LAST Annotated
# metadata item so pydantic honors it for the WHOLE union (an inner placement
# would only cover the non-null arm). "default": null semantics survive —
# the marker's schema merges with the Field default (pinned by tests). The
# REM-F BeforeValidator coercions are COMPOSED, not replaced: the union still
# validates X through the tolerant arm and null through the None arm.


class _PlainJsonSchema:
    """Schema marker: advertise a fixed plain shape for the WHOLE Optional union.

    Implements pydantic's ``__get_pydantic_json_schema__`` protocol. Used only
    at the MCP tool boundary to flatten ``anyOf[X, null]`` wire schemas into the
    maximally-compatible type-array form; the union's RUNTIME validation (REM-F
    tolerant arm + null arm) is untouched.
    """

    def __init__(self, shape: dict[str, Any]) -> None:
        self._shape = shape

    def __get_pydantic_json_schema__(  # noqa: D105 - pydantic protocol, not docstring-able
        self,
        schema: Any,
        handler: Any,
    ) -> dict[str, Any]:
        return dict(self._shape)


#: The flattened advertised shapes (the only wire-schema change REM-G makes).
#: D5 (ORVEX-CORTEX-056-LIVEFIX) WIDENED four of them to flat type-arrays that
#: ALSO admit "string" (and "object" for follow_ups): the live client-side ajv
#: validator rejected numeric-string coordinates, string allowlists, string/dict
#: follow_ups, and bare-string keys BEFORE the bytes ever reached Cortex, so the
#: REM-F server-side coercions for those shapes were unreachable from the host.
#: The runtime coercions already exist and already reject garbage fail-closed —
#: this is SCHEMA-WIDENING ONLY (still no anyOf; REM-G pin A holds).
_PLAIN_NULLABLE_INTEGER = _PlainJsonSchema({"type": ["integer", "null"]})
_PLAIN_NULLABLE_STRING = _PlainJsonSchema({"type": ["string", "null"]})
_PLAIN_NULLABLE_BOOLEAN = _PlainJsonSchema({"type": ["boolean", "null"]})
_PLAIN_NULLABLE_STRING_ARRAY = _PlainJsonSchema(
    {"type": ["array", "null"], "items": {"type": "string"}}
)
_PLAIN_NULLABLE_OBJECT_ARRAY = _PlainJsonSchema(
    {"type": ["array", "null"], "items": {"type": "object", "additionalProperties": True}}
)
#: D5 widened shapes (WO-1..WO-4): coordinate scalars admit numeric strings;
#: allowlist arrays admit comma/space-separated strings; the follow_ups queue
#: admits JSON-encoded strings and a single wrapped dict; keys admits one bare
#: key-name string or a JSON-array string. Garbage still fails typed server-side.
_PLAIN_WIDENED_NULLABLE_COORDINATE = _PlainJsonSchema(
    {"type": ["integer", "string", "null"]}
)
_PLAIN_WIDENED_NULLABLE_STRING_ARRAY = _PlainJsonSchema(
    {"type": ["array", "string", "null"], "items": {"type": "string"}}
)
_PLAIN_WIDENED_NULLABLE_OBJECT_ARRAY = _PlainJsonSchema(
    {
        "type": ["array", "object", "string", "null"],
        "items": {"type": "object", "additionalProperties": True},
    }
)
_PLAIN_NULLABLE_STRING_MAP = _PlainJsonSchema(
    {"type": ["object", "null"], "additionalProperties": True}
)
# A1.3: the ``computer_zoom`` region — an advertised 4-integer array in canonical
# screenshot space. The runtime validation is STRICTER than the wire schema (typed
# ``invalid_region`` for every violation class, A1.3 item 2 — never a clipped guess).
_PLAIN_NULLABLE_REGION = _PlainJsonSchema(
    {"type": ["array", "null"], "items": {"type": "integer"}, "minItems": 4, "maxItems": 4}
)
#: WP-E E1: the ``computer_execute`` ``record`` object — start a background
#: screen recording AROUND this call's input (same bands/validation as
#: computer_record, one code path, same typed rejections; an out-of-band value
#: is a typed rejection of the whole call BEFORE any input).
_RECORD_PARAM_PLAIN_SCHEMA = _PlainJsonSchema(
    {
        "type": ["object", "null"],
        "properties": {
            "duration_s": {"type": "number"},
            "region": {
                "type": ["array", "null"],
                "items": {"type": "integer"},
                "minItems": 4,
                "maxItems": 4,
            },
            "fps": {"type": ["integer", "null"]},
            "max_keyframes": {"type": ["integer", "null"]},
            "include_video": {"type": ["boolean", "null"]},
            "save": {"type": ["boolean", "null"]},
            # DELTA 3 NIT: NO enum — no other Cortex tool schema uses one, and
            # some hosts' schema subsets reject a null inside enum (which
            # could make the whole tool fail to register).  The allowed
            # values ("file" default | "inline"; null = the default) are
            # documented here and enforced by the runtime typed rejection
            # (``invalid_video_delivery``).
            "video_delivery": {
                "type": ["string", "null"],
                "description": (
                    'video delivery: "file" (the default — the MP4 is written '
                    'under the recordings folder and the entry carries its '
                    'path) or "inline" (the legacy embedded video/mp4 '
                    "resource); null = the default \"file\""
                ),
            },
        },
        # DELTA 1 n4/m3: unknown keys are rejected — at the schema level here,
        # and (for direct callers) at runtime with the typed
        # ``invalid_record_param`` rejection naming the allowed keys.
        "additionalProperties": False,
    }
)
_PLAIN_NULLABLE_NUMBER_MAP = _PlainJsonSchema(
    {"type": ["object", "null"], "additionalProperties": {"type": "number"}}
)


def _coerce_tolerant_keys(value: Any) -> list[str] | None:
    """Boundary coercion for the ``keys`` parameter (REM-G weak-model tolerance).

    Accepts list[str] as today, PLUS weak-model serializations: a bare STRING
    "enter" -> ["enter"] (a single key name) and a JSON-encoded array string
    '["ctrl","a"]' -> parsed (same pattern as the REM-F follow_ups tolerance).
    Non-JSON bracket strings and non-list/non-string values raise a typed error.
    """
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("["):
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise ValueError(f"keys string is not a valid JSON array: {exc}") from None
            if not isinstance(parsed, list):
                raise ValueError("keys JSON string must encode an ARRAY of key names.")
            value = parsed
        elif not stripped:
            return []
        else:
            return [stripped]
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    # ValueError (not TypeError): pydantic converts only ValueError/AssertionError
    # inside BeforeValidator into a typed ValidationError — a raw TypeError would
    # escape the validation boundary as a crash.
    raise ValueError(
        f"keys must be an array of key names (or one key name as a string), "
        f"got {type(value).__name__}."
    ) from None


#: ``keys`` tool type — runtime list[str] with REM-G string tolerance; the
#: advertised schema is flattened to array-of-string|null (see _PlainJsonSchema).
TolerantKeys = Annotated[list[str], BeforeValidator(_coerce_tolerant_keys)]


#: Optional tool parameter types with FLATTENED advertised schemas (REM-G). Each
#: composes the EXISTING REM-F tolerant runtime type with the plain-union marker:
#: runtime validation is byte-identical, only the wire schema loses anyOf.
#: D5 (WO-1..WO-4) swaps in the WIDENED markers on exactly the four friction
#: classes observed live (coordinates, allowlists, follow_ups, keys) so the
#: client-side validator stops rejecting shapes the server already tolerates.
#: NullableInt keeps the strict integer-only ADVERTISED shape for any non-tool
#: consumer; the four D5 friction classes use the widened aliases below.
NullableInt = Annotated[TolerantInt | None, _PLAIN_NULLABLE_INTEGER]
NullableStr = Annotated[str | None, _PLAIN_NULLABLE_STRING]
NullableBool = Annotated[bool | None, _PLAIN_NULLABLE_BOOLEAN]
NullableCoordinate = Annotated[TolerantInt | None, _PLAIN_WIDENED_NULLABLE_COORDINATE]
NullableStrList = Annotated[TolerantStrList | None, _PLAIN_WIDENED_NULLABLE_STRING_ARRAY]
NullableFollowUps = Annotated[TolerantFollowUps | None, _PLAIN_WIDENED_NULLABLE_OBJECT_ARRAY]
# The S-10 typed advertised schema from plan.py is attached here. The ADVERTISED
# shape is the object-only PLAN_PARAM_PLAIN_SCHEMA (no
# anyOf/$ref, no "string" arm — the REM-G plain rule and S-10 hold verbatim); the
# JSON-string tolerance is RUNTIME-only, behind the boundary, via the same
# parse-only coercion the follow_ups queue uses (the strict fail-closed validation
# itself stays in computer_execute's handler so a malformed plan is the teaching
# ``invalid_action`` reply, never an argument error).
def _coerce_plan_param(value: Any) -> Any:
    """Boundary tolerance for the ``plan`` parameter (the follow_ups pattern).

    Accepts the plan object as-is and a JSON-encoded STRING containing it; anything
    else raises a typed error before the tool runs. This helper PARSES only — the
    strict :func:`computer_use_mcp.plan.coerce_tolerant_plan` pipeline (raw
    boundary -> strict model -> model defense -> SPEC 5.2 explicitness) applies in
    the handler regardless of the wire shape, so tolerance is never a bypass.
    """
    if value is None or isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"plan string is not valid JSON: {exc}") from None
        if not isinstance(parsed, dict):
            raise ValueError("plan string must encode a JSON object")
        return parsed
    raise ValueError(
        f"plan must be an object: {{\"points\": [{{\"x\", \"y\", \"actions\"}}]}}; "
        f"got {type(value).__name__}."
    ) from None


NullablePlan = Annotated[
    dict[str, Any] | None,
    BeforeValidator(_coerce_plan_param),
    _PlainJsonSchema(plan_layer.PLAN_PARAM_PLAIN_SCHEMA),
]
NullableStrMap = Annotated[dict[str, Any] | None, _PLAIN_NULLABLE_STRING_MAP]
NullableNumberMap = Annotated[dict[str, float] | None, _PLAIN_NULLABLE_NUMBER_MAP]
NullableKeys = Annotated[TolerantKeys | None, _PLAIN_WIDENED_NULLABLE_STRING_ARRAY]
# A1.3: the zoom region [left, top, width, height] (canonical screenshot space).
NullableRegion = Annotated[list[Any] | None, _PLAIN_NULLABLE_REGION]
#: WP-E E1: the execute surface's ``record`` object (plain object schema, no
#: anyOf/$ref — the REM-G rule).
NullableRecord = Annotated[dict[str, Any] | None, _RECORD_PARAM_PLAIN_SCHEMA]


def _coerce_follow_up_entry(item: dict[str, Any]) -> dict[str, Any]:
    """Apply the same coordinate coercion INSIDE one follow_ups entry (REM-F).

    The boundary validates the queue as list[dict[str, Any]]; the strict
    :class:`~computer_use_mcp.models.ActionSpec` (unchanged) then rejects a float
    ``x`` the same way the tool parameter used to. This helper reuses the parameter
    coercion on the coordinate fields only — the strict internal model still
    enforces every bound and rejects garbage identically.
    """
    if not isinstance(item, dict):
        return item  # the strict model produces the typed failure
    coerced = dict(item)
    for name in _SPEC_COORDINATE_FIELDS:
        if name in coerced and coerced[name] is not None:
            try:
                coerced[name] = _coerce_tolerant_int(coerced[name], field=name)
            except ValueError:
                return item  # let the strict model emit its own typed error
    return coerced


# --- teach-in-text action vocabulary (PERF-004 C6) ------------------------------------------
# The EXACT ActionType vocabulary from models.py (verbatim enum values):
# click, double_click, right_click, drag, type, keypress, scroll, wait, done, move,
# hotkey, focus_window. There is deliberately NO 'key' and NO 'triple_click' action.

#: The precise valid action list, taught in tool descriptions and error messages.
ACTION_VOCABULARY = (
    "Valid actions (exact names): "
    "click (x,y required), double_click (x,y), right_click (x,y; opens a context menu), "
    "drag (x,y start + x2,y2 end, both required), "
    "move (x,y), type (text required), keypress (keys=[\"<one key name>\"]), "
    "hotkey (keys=[2-12 key names], e.g. [\"ctrl\",\"a\"]), "
    "scroll (delta -20..20; POSITIVE = UP, away from you — the wheel turns at the "
    "CURRENT CURSOR POSITION, so move the pointer to the target region first), "
    "wait (delta seconds; this flat action is capped at 10 s and reports "
    "waited_seconds when the cap bites — for a LONG wait use the computer_wait tool, "
    "not a large delta), focus_window (target = window title), "
    "ensure_app (target = \"process\" or \"process|doc-token\"; attaches to an EXISTING "
    "instance — REATTACHED/AMBIGUOUS_INSTANCE/NO_INSTANCE — and may launch the target "
    "server-side when no instance matches and the launch=\"server\" policy holds (the "
    "default; the process allowlist restricts launches only when one is configured — "
    "with NO process allowlist any resolvable target may launch; "
    "CORTEX_ATTACH_OR_LAUNCH=driver restores never-launch)), done. "
    # WP2 (ORVEX-2026-107): the vocabulary must not disagree with the three production
    # guards, which all read "hold_ms is only valid on click/right_click/keypress/
    # hotkey actions" (models.py, backend.py, validator.py). Kept to the enforced
    # rule: the legal set, the press/hold/release reading, the reverse-order hotkey
    # release, ``0`` as an explicit zero hold, and omitted-is-legacy.
    "hold_ms (0..5000 ms, optional, carried on an individual action — a follow_ups "
    "entry) is valid on click/right_click/keypress/hotkey ONLY and is rejected on "
    "every other action, including double_click, move, scroll and type: the input is "
    "pressed, stays down for the duration, then released — on a hotkey every named "
    "key goes down SIMULTANEOUSLY and comes up in REVERSE order — never a chord "
    "followed by a wait — and hold_ms=0 is that same path with a zero-duration hold, "
    "while omitting it keeps the ordinary immediate dispatch."
)

#: Key-name rule taught alongside the vocabulary (Session 1 failure class: text sent
#: as hotkey keys).
KEY_NAME_RULE = (
    "keys must be KEY NAMES (e.g. \"ctrl\", \"a\", \"enter\", \"esc\") — never text, "
    "words, or sentences; to type text use action=\"type\" with text=\"...\". "
    "Single-key presses belong on keypress (a hotkey needs 2-12 keys)."
)


def _grounded_shape_hint(text: str) -> str | None:
    """R-18: the closest valid shape for an ActionSpec-valid but GroundedAction-invalid
    queue item.

    A follow_ups entry can pass the ActionSpec schema and still fail GroundedAction
    construction inside the queue (a target-less ``focus_window``, a half-specified
    ``drag`` with one endpoint missing, a 1-key ``hotkey``, a point-less ``move``, a
    target-less ``ensure_app``). The validator messages are matched by their stable
    prefixes so the typed rejection can teach the exact valid shape instead of
    surfacing a bare pydantic error.
    """
    if "drag actions require both point" in text:
        return (
            "A drag needs BOTH endpoints: {\"action\": \"drag\", \"x\": <start x>, \"y\": <start y>, "
            "\"x2\": <end x>, \"y2\": <end y>} — x,y alone (or x2,y2 alone) is rejected."
        )
    if "hotkey actions require 2 to 12" in text:
        return (
            "A hotkey chord needs 2-12 key names: {\"action\": \"hotkey\", \"keys\": [\"ctrl\", \"a\"]}. "
            "For a single key use {\"action\": \"keypress\", \"keys\": [\"<key name>\"]}."
        )
    if "focus_window actions require a non-empty target" in text:
        return (
            "focus_window requires a non-empty target window title: "
            "{\"action\": \"focus_window\", \"target\": \"<window title>\"}."
        )
    if "move actions require a point" in text:
        return "{\"action\": \"move\", \"x\": <x>, \"y\": <y>} — move needs both coordinates."
    if "ensure_app actions require a non-empty target" in text:
        return (
            "ensure_app requires a non-empty target: {\"action\": \"ensure_app\", "
            "\"target\": \"<process>\"} or \"<process>|<doc-token>\"."
        )
    if "via is only valid on type actions" in text:
        return (
            "via selects the type-action transport only: "
            "{\"action\": \"type\", \"text\": \"...\", \"via\": \"clipboard\"} or "
            "via=\"sendinput\" (default). Remove via from every other action."
        )
    if "via must be" in text:
        return (
            "via accepts exactly \"sendinput\" (default per-character typing) or "
            "\"clipboard\" (set CF_UNICODETEXT then ctrl+v paste, for OpenGL/console "
            "apps that drop per-character input): {\"action\": \"type\", \"text\": \"...\", "
            "\"via\": \"clipboard\"}."
        )
    return None


def _teaching_invalid_action(exc: Exception, action: str | None) -> dict[str, object]:
    """Fail-closed ``invalid_action`` response that TEACHES the valid vocabulary.

    Session 1 lost full agent turns to schema rejections (``key``, 1-key ``hotkey``
    with a word payload, ``triple_click``). The rejection now always carries the exact
    valid values and, when recognizable, the closest valid shape for what was tried.
    R-18: the same teaching path covers follow_ups items that pass the ActionSpec
    schema but fail GroundedAction construction (half-specified ``drag``, 1-key
    ``hotkey``, target-less ``focus_window``/``ensure_app``, point-less ``move``).
    """
    # L2 (LIVE-WP13-02): the keys rule is only RELEVANT for actions that take
    # keys (keypress/hotkey), when the action name itself is unknown (a
    # generic malformed call), or when the validation error is ABOUT keys —
    # a wait item with a stray hold_ms (the live case) has no keys and takes
    # none; the unrelated keys hint there was noise next to the hold_ms
    # complaint.
    hints: list[str] = []
    tried = (action or "").strip().casefold()
    if (
        action is None
        or tried == ""
        or tried in {"keypress", "hotkey"}
        or "keys" in str(exc).lower()
    ):
        hints.append(KEY_NAME_RULE)
    if tried == "key":
        hints.append(
            "You sent action=\"key\", which does not exist. For a single key press use "
            "action=\"keypress\" with keys=[\"<key name>\"] (e.g. {\"action\": \"keypress\", "
            "\"keys\": [\"ctrl\"]}); for a chord use action=\"hotkey\" with 2-12 key names."
        )
    elif tried == "triple_click":
        hints.append(
            "You sent action=\"triple_click\", which does not exist. Use action=\"double_click\", "
            "or queue repeated clicks via follow_ups."
        )
    elif tried in {"context_menu", "contextmenu", "rightclick", "right-click", "rclick"}:
        hints.append(
            f"You sent action=\"{tried}\", which does not exist. The context-menu action is "
            "action=\"right_click\" with x,y (e.g. {\"action\": \"right_click\", \"x\": 640, \"y\": 360})."
        )
    elif tried in {"keypress", "hotkey"}:
        hints.append(
            "Closest valid shape for a hotkey chord: {\"action\": \"hotkey\", \"keys\": [\"ctrl\", \"a\"]} "
            "(2-12 key names). Closest valid shape for one key: {\"action\": \"keypress\", \"keys\": [\"a\"]}."
        )
    shape_hint = _grounded_shape_hint(str(exc))
    if shape_hint is not None:
        hints.append(shape_hint)
    message = f"{exc} {ACTION_VOCABULARY}"
    # §1.1 rejection envelope (ORVEX-2026-108): a malformed action is a REJECTION —
    # {ok False, result "rejected", message, reasons}, never an ``error`` key; the
    # typed ``invalid_action`` code names itself as the first reason.
    return {
        "ok": False,
        "result": "rejected",
        "message": message,
        "reasons": ["invalid_action", *hints],
    }
