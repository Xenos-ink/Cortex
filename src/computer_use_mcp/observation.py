"""Observation capture orchestration: full Observation building plus a stable digest.

Layering (master-mission section 5): this module imports only ``backend`` and ``models``
— never ``verification``/``agent``/``server``. The digest is computed locally and matches
the historical ``VerificationEngine.observation_digest`` value, so the
``computer_observe`` tool output shape (``{"observation": ..., "digest": ...}``) is
unchanged for external MCP clients (the ``text_summary`` key is additive, PERF-004 C8).

PERF-004 additions (additive only):

- :func:`observation_text_summary` — a bounded one-line structured digest for weak
  models: active window title/process, cursor position, a focused-control hint when the
  backend populated ``ui_elements`` (omitted gracefully when absent), and a
  changed/unchanged note versus the previous observation's digest. Text channels are the
  cheapest grounding signal for the host model (research digest A6).
- :func:`digest_matches` — pixel-identity proof used by the agent's digest-based
  staleness validation (PERF-004 C1): identical base64 payloads mean the screen is
  pixel-identical, so identity drift is impossible without a fresh capture.
"""

from __future__ import annotations

import hashlib
from typing import Any

from .backend import ComputerBackend
from .models import Observation


def observation_digest(observation: Observation) -> str:
    """Stable sha256 digest of an observation's base64 screenshot payload."""
    return hashlib.sha256(observation.image_base64.encode("ascii")).hexdigest()


def digest_matches(first: Observation, second: Observation) -> bool:
    """True when two observations carry pixel-identical screenshots (PERF-004 C1).

    A digest match proves the screen did not change between the two captures, so every
    identity dimension (window, process, monitor, dimensions, coordinate space) captured
    in the payload is still current. A mismatch only proves pixels changed — identity
    may still hold (benign flicker) and is then decided by the validator's identity
    staleness check.
    """
    return first.image_base64 == second.image_base64


#: Character budget for the ``text_summary`` line (bounded, cache-friendly text).
_TEXT_SUMMARY_MAX_CHARS = 400


def _focused_control_hint(observation: Observation) -> str | None:
    """Best-effort focused-control hint from backend ``ui_elements`` (omit when absent).

    The backend may populate ``ui_elements`` with structured control descriptors (dicts
    with name/control_type/focused keys) or plain strings; anything unusable degrades to
    ``None`` and the hint is omitted — never invented (PERF-004 C8: ui_elements may be
    null on every current backend).
    """
    elements = observation.ui_elements
    if not elements:
        return None
    focused: Any = None
    for element in elements[:50]:
        if isinstance(element, dict) and (
            element.get("focused") or element.get("is_focused") or element.get("has_focus")
        ):
            focused = element
            break
    if focused is None:
        candidate = elements[0]
        focused = candidate if isinstance(candidate, (dict, str)) else None
    label: str | None = None
    if isinstance(focused, dict):
        name = focused.get("name") or focused.get("title") or focused.get("label")
        control = focused.get("control_type") or focused.get("type") or focused.get("role")
        if name and control:
            label = f"{control} {name!r}"
        elif name or control:
            label = str(name or control)
    elif isinstance(focused, str) and focused.strip():
        label = focused.strip()
    if not label:
        return None
    return label[:120]


def observation_text_summary(
    observation: Observation, *, previous_digest: str | None = None
) -> str:
    """One bounded structured summary line for an observation (PERF-004 C8).

    Contains: active window title/process, cursor position, an optional focused-control
    hint (only when the backend supplied usable ``ui_elements``), and a changed /
    unchanged note versus ``previous_digest`` (``first observation`` when no previous
    digest is known). Never raises; every section degrades independently.
    """
    try:
        parts: list[str] = []
        info = observation.active_window_info
        title: str | None = None
        process: str | None = None
        if info is not None:
            title = info.title or None
            process = info.process_name or (
                info.exe_path.rsplit("\\", 1)[-1] if info.exe_path else None
            )
        if title is None:
            title = observation.active_window or None
        if title or process:
            window_desc = " | ".join(item for item in (f"process={process}" if process else None, f"title={title!r}" if title else None) if item)
            parts.append(f"window: {window_desc}")
        else:
            parts.append("window: unknown")
        if observation.cursor_x is not None and observation.cursor_y is not None:
            parts.append(f"cursor: ({observation.cursor_x}, {observation.cursor_y})")
        hint = _focused_control_hint(observation)
        if hint:
            parts.append(f"focused: {hint}")
        if previous_digest is None:
            parts.append("changed: first observation")
        else:
            same = previous_digest == observation_digest(observation)
            parts.append("changed: unchanged since previous" if same else "changed: changed since previous")
        summary = "; ".join(parts)
        return summary[:_TEXT_SUMMARY_MAX_CHARS]
    except Exception:  # noqa: BLE001 - a summary must never break an observation path
        return "summary unavailable"


class ObservationEngine:
    """Coordinates computer-state capture independently from action execution."""

    def __init__(self, backend: ComputerBackend) -> None:
        self.backend = backend

    def capture(self) -> Observation:
        """Delegate a full Observation capture to the backend (identity, timing, coordinates)."""
        return self.backend.observe()

    def capture_with_digest(self) -> tuple[Observation, str]:
        """Capture one observation plus its stable digest (the ``computer_observe`` contract)."""
        observation = self.capture()
        return observation, observation_digest(observation)
