"""FAILING reproduction (v0.5.6): the follow_ups queue stops on a
FALSE verification failure, flushing a legitimate 3-step batch.

Real-desktop occurrence (Paint "Edit colors" dialog): the driver DID batch ``click hex field + ctrl+a + type "C8C3B2" +
enter`` as follow_ups. The PRIMARY click executed fine, but the pixel-diff
verification false-failed on a focus-only change ("Expected change was not
observed: Hex input focused." -- mean pixel difference 0.013007 < threshold 1,
0 strongly-changed pixels), so ``ok=False`` and the queue stopped at index 0.
The ctrl+a / type / enter items NEVER dispatched, the driver had to re-observe
to learn that, and then burned one model turn per action for the rest of the
session ("I'll do actions one at a time from now on").

Root coupling at HEAD 19bf29b:
- ``agent.py:2626``  ``ok = verification.outcome == "verified"`` -- a
  sub-threshold (diff-blind) screen change is reported ``failed``, not
  uncertain, for keyboard actions carrying a stated ``expected_effect``
  (``verification.py:424-439``; :class:`FocusChangeStrategy` -- the REM-B H2c
  fix for this exact live failure -- is flagged for CLICK/DOUBLE_CLICK only,
  ``agent.py:801-810``).
- ``agent.py:2146-2153`` -- the queue treats ``result.ok is False`` with a
  non-uncertain verification as a DEFINITIVE stop (``verification_failed``),
  even though the action physically executed and the follow-ups re-ground from
  the fresh post-action capture anyway.

This test mirrors the live batch with a fake backend: a hotkey (ctrl+a) with a
stated effect executes fine and the screen DOES legitimately change (10 pixels
at delta 30 -- real change, but deliberately below the diff tier's noise floor:
mean ~0.033 < 1.0, strongly-changed 0 -- like a caret/selection tint). The batch
must survive; before the fix it did not.

STATUS: GREEN. The fix (agent.py `_run_action_queue`) makes
the queue CONTINUE past an EXECUTED item whose verification outcome is "failed"
(the honest failed verdict rides the per-item entry); only genuinely blocking
conditions (safety rejection, approval requirement, rejection, digest surprise,
dispatch error, no post-action observation) stop the batch.
``CORTEX_QUEUE_STRICT_VERIFY=1`` restores the v0.5.5 stop-on-failed behavior
(pinned by ``test_strict_verify_env_restores_stop_on_failed`` below).
"""

from __future__ import annotations

import base64
import io
from typing import Any

from computer_use_mcp.models import WindowInfo

import pytest
from PIL import Image

from test_controller_integration import (
    FAST_LIMITS,
    ScriptedBackend,
    ScriptedProvider,
    execute_payload,
    executed_summary,
    make_session,
)


def _subtle_change_png() -> str:
    """A white 64x48 frame with 10 pixels dimmed by delta 30 (blue band).

    Genuine screen change (10 differing pixels) that the ScreenshotDiffStrategy
    noise floor deliberately ignores: per-band means (0, 0, 0.0977) -> mean
    ~0.033 < DEFAULT_DIFF_THRESHOLD (1.0); strongly-changed (max-band delta
    >= 40): 0. This is the fake-desktop analogue of the live "Hex input
    focused" frame.
    """
    image = Image.new("RGB", (64, 48), (255, 255, 255))
    pixels = image.load()
    for x in range(10):
        pixels[x, 0] = (255, 255, 225)  # delta 30, below STRONG_PIXEL_DELTA (40)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


class SubtleChangeBackend(ScriptedBackend):
    """Executes fine; after the first action the screen changes SUBTLY.

    Like a real desktop after ctrl+a (selection tint / caret), the change is
    real but sits below the pixel-diff tier's thresholds -- the exact shape the
    live session's "Hex input focused" false failure had.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(flip=False, **kwargs)
        self._subtle_png = _subtle_change_png()

    def observe(self) -> Any:
        observation = super().observe()
        if self.executes >= 1:
            observation.image_base64 = self._subtle_png
        return observation


async def test_follow_ups_batch_survives_false_verification_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ctrl+a -> type -> enter (the live Paint batch shape) must survive a
    sub-threshold screen change: every item executes, the queue completes."""
    monkeypatch.delenv("CORTEX_QUEUE_STRICT_VERIFY", raising=False)
    backend = SubtleChangeBackend()
    provider = ScriptedProvider([])
    session_id, _bundle, _backend, _ = make_session(
        monkeypatch,
        backend=backend,
        provider=provider,
        dry_run=False,
        require_approval=False,
        limits=FAST_LIMITS,
    )
    response = await server_execute(
        session_id,
        {
            "action": "hotkey",
            "keys": ["ctrl", "a"],
            "expected_effect": "selection highlight appears",
            "follow_ups": [
                {"action": "type", "text": "C8C3B2"},
                {"action": "keypress", "keys": ["enter"]},
            ],
            "include_screenshot_after": False,
        },
    )
    payload = execute_payload(response)

    # The desired behavior: the whole 3-step batch dispatches on ONE tool call
    # and the queue completes, because an EXECUTED item whose verification is
    # inconclusive-blind must not be read as a definitive batch stop.
    assert len(executed_summary(backend)) == 3, (
        "the batch was flushed on a FALSE verification failure: only "
        f"{len(executed_summary(backend))} of 3 actions dispatched "
        f"({[item[0] for item in executed_summary(backend)]}); "
        f"stopped_reason={payload.get('follow_ups_stopped_reason')!r}; "
        f"primary ok={payload.get('ok')!r} message={payload.get('message')!r}; "
        f"verification={payload.get('verification')}"
    )
    assert payload["follow_ups_stopped_reason"] is None, payload.get(
        "follow_ups_stopped_reason"
    )
    assert len(payload["follow_up_results"]) == 3
    assert [item["action_type"] for item in payload["follow_up_results"]] == [
        "hotkey",
        "type",
        "keypress",
    ]
    # Per-item honesty : the not-verified item keeps its honest verdict
    # (ok=False + reasons + verification block) in follow_up_results — continuing the
    # batch never hides the failure (verdict-honesty: the verdict is now uncertain, not failed).
    failed_entry = payload["follow_up_results"][0]
    assert failed_entry["ok"] is False
    # verdict-honesty : a sub-threshold screen change with a stated effect is UNCERTAIN
    # (absent pixels are not proof of absence) - no longer the live session's false
    # definitive "failed"; it is still never a success.
    assert failed_entry["verification_outcome"] == "uncertain"
    assert failed_entry["message"]


async def test_strict_verify_env_restores_stop_on_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CORTEX_QUEUE_STRICT_VERIFY=1 restores the v0.5.5 stop-on-failed behavior: the
    same sub-threshold batch that now completes (above) flushes again, with the
    legacy ``verification_failed`` stop reason. Read lazily per decision — toggleable
    exactly like the CORTEX_DIFF_FAST knob."""
    monkeypatch.setenv("CORTEX_QUEUE_STRICT_VERIFY", "1")
    backend = SubtleChangeBackend(
        active_window=WindowInfo(hwnd=1, pid=10, process_name="app.exe", title="App")
    )
    provider = ScriptedProvider([])
    session_id, _bundle, _backend, _ = make_session(
        monkeypatch,
        backend=backend,
        provider=provider,
        dry_run=False,
        require_approval=False,
        limits=FAST_LIMITS,
    )
    response = await server_execute(
        session_id,
        {
            # verdict-honesty : the failing item is a deterministic window_state
            # expectation that never appears -> definitive failed (a pixel-shaped
            # stated effect would degrade to uncertain, and uncertain never stops).
            "action": "keypress",
            "keys": ["enter"],
            "expected_effect": "open Calculator",
            "follow_ups": [
                {"action": "type", "text": "C8C3B2"},
                {"action": "keypress", "keys": ["enter"]},
            ],
            "include_screenshot_after": False,
        },
    )
    payload = execute_payload(response)
    assert payload["follow_ups_stopped_reason"] == "verification_failed"
    assert len(executed_summary(backend)) == 1, "items 2-3 were flushed"
    assert payload["follow_up_results"][0]["verification_outcome"] == "failed"
    assert len(executed_summary(backend)) == 1  # the follow-ups were flushed
    monkeypatch.delenv("CORTEX_QUEUE_STRICT_VERIFY", raising=False)


async def test_real_dispatch_failure_still_stops_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A REAL failure is untouched: an action that fails to DISPATCH (the backend
    raises mid-execute -> kind="error") still stops the batch before the remaining
    items — only EXECUTED items' verification verdicts are non-blocking."""
    monkeypatch.delenv("CORTEX_QUEUE_STRICT_VERIFY", raising=False)
    backend = SubtleChangeBackend()

    def boom_on_second_action(action: Any) -> None:
        if backend.executes == 1:  # the FIRST follow-up dispatch
            raise RuntimeError("simulated dispatch failure")

    backend.execute_hooks.append(boom_on_second_action)
    provider = ScriptedProvider([])
    session_id, _bundle, _b, _ = make_session(
        monkeypatch,
        backend=backend,
        provider=provider,
        dry_run=False,
        require_approval=False,
        limits=FAST_LIMITS,
    )
    response = await server_execute(
        session_id,
        {
            "action": "hotkey",
            "keys": ["ctrl", "a"],
            "follow_ups": [
                {"action": "type", "text": "C8C3B2"},
                {"action": "keypress", "keys": ["enter"]},
            ],
            "include_screenshot_after": False,
        },
    )
    payload = execute_payload(response)
    assert payload["follow_ups_stopped_reason"] == "error", payload.get(
        "follow_ups_stopped_reason"
    )
    error_entry = payload["follow_up_results"][1]
    assert error_entry["kind"] == "error" and error_entry["ok"] is False
    assert "simulated dispatch failure" in error_entry["message"]
    assert len(executed_summary(backend)) == 1  # item 3 never ran


async def test_allowlist_rejection_message_names_the_real_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ a process-allowlist rejection no longer arrives stamped with the
    generic "Grounding rejected." — the message names the REAL gate (validation /
    process allowlist) and the offending foreground process, exactly the turn-burning
    confusion the live session paid for (it read "grounding" and started decoding
    coordinate semantics)."""
    from computer_use_mcp.models import WindowInfo

    backend = ScriptedBackend(
        active_window=WindowInfo(hwnd=1, pid=4242, process_name="zcode.exe", title="driver console")
    )
    provider = ScriptedProvider([])
    session_id, _bundle, _b, _ = make_session(
        monkeypatch,
        backend=backend,
        provider=provider,
        dry_run=False,
        require_approval=False,
        limits=FAST_LIMITS,
        allowed_processes=["mspaint.exe"],
    )
    response = await server_execute(
        session_id,
        {"action": "click", "x": 10, "y": 10, "include_screenshot_after": False},
    )
    payload = execute_payload(response)
    assert payload["ok"] is False
    assert payload["message"].startswith("Action rejected by validation:"), payload["message"]
    assert "allowlist" in payload["message"]
    assert "zcode.exe" in payload["message"]  # the offending FOREGROUND process is named
    assert payload["reasons"], "structured reasons still ride the rejection"
    assert backend.executed == []  # nothing executed


async def server_execute(session_id: str, spec: dict[str, Any]) -> Any:
    """Thin indirection over ``server.computer_execute`` (keeps the test body readable)."""
    from computer_use_mcp import server

    return await server.computer_execute(session_id, **spec)
