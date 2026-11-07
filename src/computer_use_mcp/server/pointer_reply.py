"""WP-C3 server seam (ORVEX-2026-109; DELTA 1): show_pointer resolution, the
additive ``pointer`` / ``last_click`` reply blocks, and the capture-time
``last_click`` snapshot the evidence store serves on fetch.

The compositor itself (:mod:`computer_use_mcp.vision.pointer_sprite`) is pure
Pillow; this module is the SERVER-side policy around it:

* ``resolve_show_pointer`` — param > env ``CORTEX_SHOW_POINTER`` > default
  ``True`` (fallback-not-clamp boolean env parse, reusing the desktop layer's
  ``_env_bool``); STRICT booleans only — a direct call with a non-bool
  ``explicit`` falls back to the next layer (the tool boundary REJECTS
  non-bools with the typed ``invalid_show_pointer`` error, like
  ``pixel_evidence``);
* ``session_show_pointer`` — the per-session default resolved at
  ``start_session`` and stored in ``bundle.extra``;
* ``pointer_reply_fields`` — the additive reply block (canonical coordinates)
  plus the backend ring's ``last_click`` mapped through the capture transform;
  the capture sites store ``pointer_fields["last_click"]`` as the StoredFrame's
  canonical snapshot (DELTA 1 M4), which fetch serves;
* ``executed_click_point`` — the FLAT action's click point (DELTA 1 M8: the
  reply image is item 0's post-action frame, so Pressed is decided from the
  flat action alone; follow-ups never touch it).

Nothing here touches the OS cursor. Text-mode replies and every image-less
reply never call into it.
"""

from __future__ import annotations

import time
from typing import Any

from ..desktop.backend import _env_bool
from ..desktop.coordinates import CoordinateTransform

#: Env knob name: ``CORTEX_SHOW_POINTER`` is the DEFAULT SOURCE for the session's
#: pointer sprite (the per-call ``show_pointer`` parameter wins when given).
SHOW_POINTER_ENV = "CORTEX_SHOW_POINTER"

#: Default ON: the agent sees where the pointer is unless the session (or the
#: env, or the call) opts out.
SHOW_POINTER_DEFAULT = True

#: The pointer actions that put an executed reply's image into the Pressed
#: state (WP-C3 state rules; DELTA 1 M8: the FLAT action only).
POINTER_CLICK_ACTIONS = frozenset({"click", "right_click", "double_click"})


def resolve_show_pointer(explicit: Any) -> bool:
    """param > env ``CORTEX_SHOW_POINTER`` > default True (fallback-not-clamp).

    STRICT (DELTA 1 MINOR 5): only a real ``True``/``False`` counts as the
    parameter layer; anything else (``None``, a string, ...) falls through to
    the env/default. The MCP boundary rejects non-bool parameters with the
    typed ``invalid_show_pointer`` error before this runs.
    """
    if explicit is True:
        return True
    if explicit is False:
        return False
    return _env_bool(SHOW_POINTER_ENV, SHOW_POINTER_DEFAULT)


def session_show_pointer(bundle: Any) -> bool:
    """The session's resolved default (written at start_session; True otherwise)."""
    value = getattr(bundle, "extra", {}).get("show_pointer")
    if value is True or value is False:
        return value
    return SHOW_POINTER_DEFAULT


def call_show_pointer(bundle: Any, param: Any) -> bool:
    """The per-call resolution: ``None`` means the session default."""
    if param is True or param is False:
        return param
    return session_show_pointer(bundle)


def _canonical_last_click(
    bundle: Any, transform: CoordinateTransform | None, *, now: float | None = None
) -> dict[str, Any] | None:
    """The backend ring's last completed CLICK (drag presses excluded, DELTA 1
    MINOR 3), mapped to canonical screenshot coordinates:
    ``{x, y, button, age_ms}`` or ``None`` (no ring, no press, or any accessor
    failure — the field is additive and must never break a reply).
    """
    backend = getattr(bundle, "backend", None)
    accessor = getattr(backend, "last_click", None)
    if not callable(accessor):
        return None
    try:
        click = accessor()
    except Exception:  # noqa: BLE001 - additive field, never a reply-breaker
        return None
    if not isinstance(click, dict):
        return None
    px = click.get("x")
    py = click.get("y")
    t = click.get("t")
    mapped_x: float = float(px)
    mapped_y: float = float(py)
    if transform is not None:
        try:
            mapped_x, mapped_y = transform.to_screenshot(float(px), float(py))
        except Exception:  # noqa: BLE001 - additive field, never a reply-breaker
            # Unmappable (degenerate scale): report the ring's physical coords verbatim.
            mapped_x, mapped_y = float(px), float(py)
    age_ms: float | None = None
    if isinstance(t, (int, float)):
        reference = time.monotonic() if now is None else now
        age_ms = round(max(0.0, (reference - float(t)) * 1000.0), 1)
    return {
        "x": mapped_x,
        "y": mapped_y,
        "button": click.get("button"),
        "age_ms": age_ms,
    }


def pointer_reply_fields(
    bundle: Any,
    *,
    x: float | None,
    y: float | None,
    state: str,
    in_image: bool,
    transform: CoordinateTransform | None = None,
) -> dict[str, Any]:
    """The additive reply block for an image-bearing reply (WP-C3).

    ``x``/``y`` are the pointer position in CANONICAL screenshot coordinates
    (the capture-time ``cursor_x/y`` read, or the executed click point); the
    block reports them verbatim together with the drawn ``state`` and whether
    the hotspot landed inside the served image. ``last_click`` comes from the
    backend's pointer ring, mapped through ``transform`` when one is supplied
    (the capture's CoordinateTransform). NOTE (DELTA 1 M8): the ring is read at
    REPLY time, so on a follow-up call ``last_click`` may name a LATER
    follow-up click than the frame's own Pressed point — acceptable, because
    ``last_click`` is reported separately with its own coordinates.
    """
    fields: dict[str, Any] = {
        "pointer": {
            "x": x,
            "y": y,
            "state": state,
            "in_image": bool(in_image),
        }
    }
    fields["last_click"] = _canonical_last_click(bundle, transform)
    return fields


def action_value(action: Any) -> str:
    """The action name of a GroundedAction/ActionSpec (enum-safe)."""
    value = getattr(action, "action", None)
    return getattr(value, "value", value)


def executed_click_point(flat_action: Any) -> tuple[int, int] | None:
    """The click point of one click-family action, or ``None`` (DELTA 1 M8).

    The reply image is item 0's post-action frame, so Pressed is decided from
    the flat action ONLY — follow-ups (and their rejections) never touch it.
    The point is canonical screenshot coordinates. Accepts BOTH shapes the
    surface uses: GroundedAction (``point``) and ActionSpec (``x``/``y`` — the
    plan-step slices hold specs, DELTA 1 M8a).
    """
    if flat_action is None:
        return None
    if action_value(flat_action) not in POINTER_CLICK_ACTIONS:
        return None
    point = getattr(flat_action, "point", None)
    if point is not None:
        return (int(point.x), int(point.y))
    x = getattr(flat_action, "x", None)
    y = getattr(flat_action, "y", None)
    if x is not None and y is not None:
        return (int(x), int(y))
    return None


def executed_pointer_fields(
    bundle: Any,
    result: Any,
    flat_action: Any,
) -> tuple[tuple[int, int] | None, str, tuple[float, float, str] | None, dict[str, Any] | None]:
    """The whole execute/plan-step pointer decision in one place (DELTA 1 M7/M8).

    Returns ``(pointer_xy, pointer_state, pointer, pointer_fields)``:
    Pressed at the FLAT action's click point when it is a click-family action
    (the reply image is item 0's post-action frame), Normal at the capture-time
    cursor (``result._pointer``, stashed by the pipeline) otherwise; ``None``
    everywhere when the pointer is unknown or the session default is off. The
    ``pointer_fields["pointer"]["in_image"]`` value is the honest ``False``
    placeholder — ``_execute_response_blocks`` overwrites it with composite()'s
    real verdict for the served copy. Canonical coordinates (ints) throughout.
    """
    show = session_show_pointer(bundle)
    click_point = executed_click_point(flat_action)
    captured_pointer = getattr(result, "_pointer", None)
    pointer_transform = getattr(result, "_pointer_transform", None)
    pointer_xy: tuple[int, int] | None = None
    pointer_state = "normal"
    if click_point is not None:
        pointer_xy = click_point
        pointer_state = "pressed"
    elif (
        isinstance(captured_pointer, tuple)
        and len(captured_pointer) == 2
        and captured_pointer[0] is not None
        and captured_pointer[1] is not None
    ):
        pointer_xy = (int(captured_pointer[0]), int(captured_pointer[1]))
    pointer: tuple[float, float, str] | None = None
    fields: dict[str, Any] | None = None
    if show and pointer_xy is not None:
        pointer = (pointer_xy[0], pointer_xy[1], pointer_state)
        fields = pointer_reply_fields(
            bundle,
            x=pointer_xy[0],
            y=pointer_xy[1],
            state=pointer_state,
            in_image=False,
            transform=pointer_transform,
        )
    return pointer_xy, pointer_state, pointer, fields
