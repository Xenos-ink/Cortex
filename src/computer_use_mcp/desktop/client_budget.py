"""WP13 (Addendum 4): the HOST's per-tool-call timeout budget — one knob, every
long-blocking bound derived from it.

Field evidence (ZCode session sess_80396a19): the host cuts a tool call off at
30 s, while Cortex assumed a 60 s client. ``CORTEX_CLIENT_TIMEOUT_SECONDS``
names the host's real per-tool-call timeout; with a FIXED safety margin
(``CLIENT_TIMEOUT_MARGIN_SECONDS`` = 15 s, the same 60 -> 45 margin the product
shipped with) it derives every long-blocking bound:

- the type-action deadline (``type_action_deadline_seconds``: 60 -> 45 as
  shipped, 30 -> 15) and the paced-dispatch share of it
  (``pacing_budget_seconds``: 80% of the deadline);
- the blocking-segment default of the wait family
  (``wait_segment_default_seconds``: 60 -> 45 as shipped, 30 -> 15), clamped to
  >= 1 s.

The env knob is read at CALL time (not import time) so tests can monkeypatch the
environment per case. Out-of-band, unparsable and non-finite values FALL BACK to
the default (the house knob style) — a typo can never arm a bound outside the
validated 10..600 s band. Documented in docs/CONFIGURATION.md.
"""

from __future__ import annotations

import math
import os

CLIENT_TIMEOUT_ENV = "CORTEX_CLIENT_TIMEOUT_SECONDS"
CLIENT_TIMEOUT_DEFAULT_SECONDS = 60.0
CLIENT_TIMEOUT_MIN_SECONDS = 10.0
CLIENT_TIMEOUT_MAX_SECONDS = 600.0
#: The FIXED safety margin between the host timeout and every derived bound
#: (60 -> 45 as shipped; the same margin produces 15 s bounds at a 30 s host).
CLIENT_TIMEOUT_MARGIN_SECONDS = 15.0
#: Floor for the derived type-action deadline (a 10 s host still gets a 5 s
#: deadline — pacing and verification degrade, but the ladder stays usable).
TYPE_ACTION_DEADLINE_FLOOR_SECONDS = 5.0


def client_timeout_seconds() -> float:
    """The host's per-tool-call timeout: env override, else 60 s. Out-of-band,
    unparsable or non-finite values fall back to the default."""
    raw = os.environ.get(CLIENT_TIMEOUT_ENV, "").strip()
    if not raw:
        return CLIENT_TIMEOUT_DEFAULT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return CLIENT_TIMEOUT_DEFAULT_SECONDS
    if not math.isfinite(value):
        return CLIENT_TIMEOUT_DEFAULT_SECONDS
    if value < CLIENT_TIMEOUT_MIN_SECONDS or value > CLIENT_TIMEOUT_MAX_SECONDS:
        return CLIENT_TIMEOUT_DEFAULT_SECONDS
    return value


def type_action_deadline_seconds() -> float:
    """Derived per-action deadline: client timeout minus the fixed safety margin
    (60 -> 45 as shipped; 30 -> 15), floored at 5 s."""
    return max(
        TYPE_ACTION_DEADLINE_FLOOR_SECONDS,
        client_timeout_seconds() - CLIENT_TIMEOUT_MARGIN_SECONDS,
    )


def pacing_budget_seconds() -> float:
    """The paced-dispatch share of the derived deadline (80%); the rest goes to
    verification, the horizon and a possible repair."""
    return round(type_action_deadline_seconds() * 0.8, 3)


def wait_segment_default_seconds() -> float:
    """Derived wait-segment default: client timeout minus the fixed safety
    margin (60 -> 45 as shipped; 30 -> 15), floored at 1 s. An explicit
    ``CORTEX_WAIT_SEGMENT_SECONDS`` override still wins INSIDE its band
    [1, client timeout − 5]; an out-of-band value falls back to this derived
    default (fallback-not-clamp) — see server.wait_machine._wait_segment_seconds."""
    return max(1.0, client_timeout_seconds() - CLIENT_TIMEOUT_MARGIN_SECONDS)


#: Wall bound for ONE UIA semantic read (see desktop.text_readers): min(2 s,
#: timeout/10, floored at 0.5 s) — 2 s at the shipped defaults, 2 s at a 30 s
#: host, 1 s at a 10 s host. A single cross-process COM call into a hung
#: WebView2/Chromium UIA provider can block forever; the harness runs the read
#: on the reader's single OWNER thread and abandons the request past this wall
#: (the caller returns).
SEMANTIC_READ_CAP_SECONDS = 2.0
#: Wall bound for the ONE-TIME backend warm (CoInitializeEx + CoCreateInstance
#: + one focused read): the full derived call-budget share, capped at 10 s — a
#: cold first CoCreateInstance on a slow or loaded machine can exceed the
#: per-read wall, and a warm timeout must NOT silently swap the session to the
#: Win32 reader (WP13 DELTA 2).
SEMANTIC_WARM_CAP_SECONDS = 10.0


def semantic_read_timeout_seconds() -> float:
    """Wall bound for one UIA semantic read/warm (derived, call-time read).

    min(SEMANTIC_READ_CAP_SECONDS, max(0.5, client_timeout_seconds() / 10.0)):
    2.0 at the default 60 s host and at a 30 s host, 1.0 at a 10 s host
    (10/10), never below the 0.5 s floor."""
    return min(SEMANTIC_READ_CAP_SECONDS, max(0.5, client_timeout_seconds() / 10.0))


def semantic_warm_timeout_seconds() -> float:
    """Wall bound for the one-time UIA warm: the FULL derived call budget
    (``type_action_deadline_seconds()``) capped at
    ``SEMANTIC_WARM_CAP_SECONDS`` — 10 s at the shipped 60 s host, 10 s at a
    30 s host (deadline 15), 5 s at a 10 s host (deadline 5)."""
    return min(SEMANTIC_WARM_CAP_SECONDS, type_action_deadline_seconds())
