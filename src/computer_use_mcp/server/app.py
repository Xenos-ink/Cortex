"""MCP server wiring: 5 deterministic tools, bounded session registry, kill path, audit, limits.

Compatibility contract (master-mission section 6, binding, as amended 2026-09 by user
order — the internal-LLM loop tool family is REMOVED PERMANENTLY):

- The exposed tool surface is exactly FIVE deterministic tools: ``start_session``,
  ``stop_session``, ``computer_observe``, ``computer_screenshot``,
  ``computer_execute``. The internal-LLM loop tool family is deleted — the
  host model drives the tools directly; no model-decides loop remains in Cortex.
  [AMENDMENT A1 (AL-002, owner addendum 2026-09-18): a SIXTH additive tool
  ``computer_zoom`` joins the observation family — observation-only (no action
  surface), fresh capture per call, fail-closed region validation; it changes no
  pre-existing tool's default behavior.]
- Tool names, stdio transport, and parameter positions are preserved; signatures
  gain TRAILING OPTIONAL params only (``start_session(..., allowed_processes=None,
  limits=None)``, ``computer_execute(..., expected_effect=None, include_screenshot_after=None,
  follow_ups=None)``).
- Existing return shapes keep their top-level keys; new fields are additive only.
- SANCTIONED DEFAULT CHANGE (PERF-004 C5, documented intentional policy change):
  ``start_session`` now defaults to ``dry_run=False`` (Session 1 forensics: the old
  ``dry_run=True`` default silently produced no-op sessions that cost a full agent
  turn). ``require_approval`` still defaults to True. Every dry-run result message
  starts with the unmistakable banner ``DRY-RUN (no input dispatched):``.
- ``computer_execute`` keeps the hardcoded ``confidence=1.0`` — redefined as the
  client-asserted MODEL confidence for a direct caller action; grounding confidence,
  staleness, risk classification, approval, and verification apply independently.
  ``include_screenshot_after=False`` (additive) omits the heavy
  ``screenshot_after_base64`` from the response; omitted or None ships the DEFAULT
  image as a HALF-RESOLUTION JPEG (0.5x q60 — full resolution via an
  explicit ``include_screenshot_after=true``, ``computer_observe``, or
  ``CORTEX_ACTION_IMAGE_FULL=1``). ``follow_ups`` (additive, max 5) queues actions
  that each pass the FULL independent pipeline; the queue stops only on genuinely
  blocking conditions (rejection, safety, approval, attached-window staleness —
  ordinary pixel changes from earlier items do not stop it; dispatch
  error) — an executed item's uncertain OR failed verification verdict rides its
  per-item entry while the batch continues (``CORTEX_QUEUE_STRICT_VERIFY=1``
  restores stop-on-failed, ``CORTEX_QUEUE_STRICT_DIGEST=1`` the whole-screen stop).
- ``stop_session`` keeps its signature/return shape; it now arms the thread-safe
  StopToken kill path and audits stop + emergency_stop.
- ``start_session`` succeeds with no API key (provider construction is lazy at the first
  model call — compat decision 5; dry-run usable).
- REM-F weak-model boundary tolerance (ORVEX-CORTEX-055, live-test hardening):
  ``x``/``y``/``x2``/``y2`` accept integral floats and numeric strings and ROUND
  non-integral values; ``follow_ups`` accepts a JSON-encoded string / single dict /
  JSON-string entries; ``allowed_processes``/``allowed_windows`` accept
  comma/space-separated or bare strings. All via Annotated BeforeValidator
  coercions PRE-validation — the advertised schemas stay "integer"/"array", the
  downstream internal models stay strict, and garbage still fails typed.
  ``limits`` is deliberately NOT made tolerant (fail-closed numeric contract).

- Image delivery (ORVEX-CORTEX-056-LIVEFIX, D1, trailing optional):
  ``start_session(..., image_delivery=None)`` — "image" (default) keeps real image
  blocks; "text" suppresses every OUTBOUND image block so non-vision models stay
  alive (one ImageContent in their history kills the provider request with a 400).
  The start_session docstring teaches judging by WHAT THE MODEL RECEIVES (not
  self-identity): text inputs → pass "text" (required); unsure → "text" (a vision
  model in text mode only loses pixels, a text-only model in image mode dies).
  Precedence: param > env ``CORTEX_IMAGE_DELIVERY`` (fail-safe: only exact
  "text"/"image" honored, garbage/unset → "image") > default "image"; an invalid
  param value fails closed with ``invalid_image_delivery`` BEFORE any session is
  created; ``include_screenshot_after`` can only remove bytes, never re-add them.

Test/extension seam (for E6/E7): the module-level ``_backend_factory`` and
``_provider_factory`` callables are invoked once per ``start_session``; tests monkeypatch
them to inject ``FakeComputerBackend``/scripted providers, and read session wiring via
``_get_bundle(session_id)`` (agent, state, backend, enforcer, auditor, metrics).
"""

from __future__ import annotations

import asyncio

# Registration order (plan.md section 4): tool modules imported in the exact source
# order of the ten @mcp.tool() decorators of the old monolith. lifecycle FIRST also
# initializes instance/schemas/state/replies/wait_machine/plan_surface before any
# registration (matches the old 161-163 -> 183-195 -> 3643 -> registrations order).
from . import lifecycle  # start_session, stop_session
from . import wait  # computer_wait, computer_wait_status, computer_cancel_wait
from . import observe  # computer_observe, computer_screenshot, computer_zoom
from . import execute  # computer_execute
from . import fetch  # computer_fetch_image
from .instance import mcp


def main() -> None:
    asyncio.run(mcp.run_stdio_async())

