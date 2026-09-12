"""R-1 red-team matrix (ORVEX-CORTEX-057-REALMCP) — INDEPENDENT regression attacks.

R-1 did not write the v0.5.6 fixes; this file exists to BREAK them. Every test is
one attack from the R-1 mandate, run against fakes only (no real screen, no MCP
server launch, no process spawn):

  1. Real-defect detection preserved: an UNFLAGGED visual-change intent whose
     transition genuinely does not occur must STILL be a definitive
     ``failed``/ok=False (legacy semantics), and under
     ``CORTEX_QUEUE_STRICT_VERIFY=1`` must still stop the batch.
  2. Honesty under continue: dispatch errors still stop the batch (only
     verification-failed continues); a verification-failed-but-executed middle
     item continues AND its ``follow_up_results`` entry carries ok=False + the
     verification evidence (no silent swallowing).
  3. Uncertain never upgrades: a chain whose strategies all abstain must end
     ``uncertain`` (flagged AND unflagged), ok stays False — the result must
     still tell the driver the item did NOT verify (no ok=true lie).
  4. Env knob hygiene: ``CORTEX_QUEUE_STRICT_VERIFY`` unset/garbage/"0" ->
     continue; "1" -> stop; lazy per-use read (toggling between batches works).
  5. Message audit: allowlist + staleness rejections name the ACTUAL gate and
     never carry the blanket "Grounding rejected." stamp.
  6. H3 at HEAD: under DEFAULT policy, ``ensure_app`` on a NO_INSTANCE for an
     allowlisted target results in a SERVER-SIDE launch dispatch (fake backend
     records the launch; the real-desktop contract is mirrored by
     ``FakeComputerBackend.ensure_app``), and ``CORTEX_ATTACH_OR_LAUNCH=driver``
     restores never-launch.
  7. Capture floor: the flagged-uncertain degrade adds ZERO captures over the
     unflagged path (identical observation counts, same phase sequence).

Batch shape convention here is DELIBERATELY different from the implementer's
repro file (click-primary batches, middle-item failures, parametrized knob
values) so a shared scaffolding bug cannot mask a shared test bug.
"""

from __future__ import annotations

from typing import Any

import pytest

from computer_use_mcp import server
from computer_use_mcp.backend import FakeComputerBackend
from computer_use_mcp.models import WindowInfo

from test_controller_integration import (
    FAST_LIMITS,
    fresh_server,
    ScriptedBackend,
    ScriptedProvider,
    audit_events,
    execute_payload,
    executed_summary,
    make_session,
)

QUEUE_ENV = "CORTEX_QUEUE_STRICT_VERIFY"
LAUNCH_ENV = "CORTEX_ATTACH_OR_LAUNCH"


class _EnsureAppRecordingBackend(ScriptedBackend):
    """ScriptedBackend with the REAL 4-arg ``execute`` contract.

    ``agent._backend_execute`` ALWAYS passes ``focus_hook``/``allow_launch`` kwargs
    for ensure_app actions (agent.py:441-448); the shared ScriptedBackend fake keeps
    the legacy 2-arg signature, so the ensure_app dispatch surfaces as a TypeError
    there. This fake binds the modern signature and routes the probe through
    ``FakeComputerBackend.execute`` -> ``FakeComputerBackend.ensure_app`` — which
    mirrors LocalComputerBackend.ensure_app's NO_INSTANCE launch dispatch
    (backend.py:3732-3743) by recording ``launched_processes``.
    """

    def execute(
        self,
        action: Any,
        stop: Any = None,
        focus_hook: Any = None,
        allow_launch: bool = False,
    ) -> str:
        for hook in self.execute_hooks:
            hook(action)
        if action.action.value == "type" and action.text:
            self.typed_text = action.text
        message = FakeComputerBackend.execute(
            self, action, stop, focus_hook=focus_hook, allow_launch=allow_launch
        )
        self.executes += 1
        return message


async def _run(session_id: str, spec: dict[str, Any]) -> dict[str, Any]:
    """computer_execute + payload unwrap (executed responses arrive as MCP blocks)."""
    return execute_payload(await server.computer_execute(session_id, **spec))


def _session(monkeypatch: pytest.MonkeyPatch, backend: Any, **start: Any) -> str:
    monkeypatch.setattr(server, "_backend_factory", lambda: backend)
    monkeypatch.setattr(server, "_provider_factory", lambda: ScriptedProvider([]))
    response = server.start_session(
        dry_run=False, require_approval=False, limits=FAST_LIMITS, **start
    )
    return str(response["session_id"])


# --- Attack 1: real-defect detection is preserved ---------------------------------------------


async def test_attack1_stated_effect_nonchange_honest_not_success(
    monkeypatch: pytest.MonkeyPatch,
    fresh_server: Any,
) -> None:
    """Real-defect detection preserved under RC-D11 (058). A hotkey with a
    pixel-shaped stated effect on a screen that genuinely does not change is NO
    LONGER a definitive false "failed" (W-1-era semantics): it degrades to
    ``uncertain`` 0.4 — absent pixels are not proof of absence — but it is NEVER
    reported as success (ok=False) and the input still dispatched. The DEFINITIVE
    real-failure verdict survives one tier over: a deterministic window_state
    expectation that never appears still reports ``failed``."""
    monkeypatch.delenv(QUEUE_ENV, raising=False)
    backend = ScriptedBackend(flip=False)  # every frame identical: no transition, truly
    session_id = _session(monkeypatch, backend)
    payload = await _run(
        session_id,
        {
            "action": "hotkey",
            "keys": ["ctrl", "a"],
            "expected_effect": "selection highlight appears",
            "include_screenshot_after": False,
        },
    )
    assert payload["ok"] is False, payload  # uncertain is never success
    verification = payload["verification"]
    assert verification["outcome"] == "uncertain", verification
    assert abs(verification["confidence"] - 0.4) < 1e-9, verification
    assert verification["changed"] is False
    assert backend.executed, "the input still dispatched (the verdict is about verification)"
    # The deterministic tier still detects a REAL failure definitively.
    backend2 = ScriptedBackend(
        flip=False,
        active_window=WindowInfo(hwnd=1, pid=10, process_name="app.exe", title="App"),
    )
    session_id2 = _session(monkeypatch, backend2)
    failed = await _run(
        session_id2,
        {
            "action": "keypress",
            "keys": ["enter"],
            "expected_effect": "open Calculator",  # the title never appears
            "include_screenshot_after": False,
        },
    )
    assert failed["ok"] is False
    assert failed["verification"]["outcome"] == "failed", failed.get("verification")


async def test_attack1b_unflagged_failed_still_stops_batch_under_strict_env(
    monkeypatch: pytest.MonkeyPatch,
    fresh_server: Any,
) -> None:
    """CORTEX_QUEUE_STRICT_VERIFY=1 restores the v0.5.5 stop: the same genuinely-
    unchanged batch flushes its follow-ups with the legacy stop reason."""
    monkeypatch.setenv(QUEUE_ENV, "1")
    backend = ScriptedBackend(
        flip=False,
        active_window=WindowInfo(hwnd=1, pid=10, process_name="app.exe", title="App"),
    )
    session_id = _session(monkeypatch, backend)
    payload = await _run(
        session_id,
        {
            # RC-D11 (058): the failing item is a deterministic window_state
            # expectation that never appears -> definitive failed (a pixel-shaped
            # stated effect would degrade to uncertain and never stop the batch).
            "action": "keypress",
            "keys": ["enter"],
            "expected_effect": "open Calculator",
            "follow_ups": [
                {"action": "type", "text": "r1"},
                {"action": "keypress", "keys": ["enter"]},
            ],
            "include_screenshot_after": False,
        },
    )
    assert payload["follow_ups_stopped_reason"] == "verification_failed", payload.get(
        "follow_ups_stopped_reason"
    )
    assert len(executed_summary(backend)) == 1  # items 2-3 were flushed
    entry = payload["follow_up_results"][0]
    assert entry["ok"] is False and entry["verification_outcome"] == "failed"


# --- Attack 2: honesty under continue ----------------------------------------------------------


async def test_attack2a_middle_dispatch_error_still_stops_batch(
    monkeypatch: pytest.MonkeyPatch,
    fresh_server: Any,
) -> None:
    """Only verification-failed continues. A middle item that fails to DISPATCH
    (backend raises) must still stop the batch: item 3 never runs, and the error
    entry stays honest (kind=error, ok=False, the fault named)."""
    monkeypatch.delenv(QUEUE_ENV, raising=False)
    backend = ScriptedBackend(flip=False)

    def explode_on_first_follow_up(action: Any) -> None:
        if backend.executes == 1:  # primary completed; the MIDDLE item now dispatches
            raise RuntimeError("r1 dispatch fault")

    backend.execute_hooks.append(explode_on_first_follow_up)
    session_id = _session(monkeypatch, backend)
    payload = await _run(
        session_id,
        {
            "action": "click",
            "x": 10,
            "y": 10,  # primary: no expectation -> uncertain -> legitimately continues
            "follow_ups": [
                {"action": "type", "text": "r1"},
                {"action": "keypress", "keys": ["enter"]},
            ],
            "include_screenshot_after": False,
        },
    )
    assert payload["follow_ups_stopped_reason"] == "error", payload.get(
        "follow_ups_stopped_reason"
    )
    assert len(executed_summary(backend)) == 1, "item 3 must never dispatch"
    results = payload["follow_up_results"]
    assert [entry["index"] for entry in results] == [0, 1]
    assert results[0]["kind"] == "executed"  # the primary ran and is reported
    error_entry = results[1]
    assert error_entry["kind"] == "error" and error_entry["ok"] is False
    assert "r1 dispatch fault" in error_entry["message"]
    assert len(results) == 2  # no fabricated entry for the never-run item


async def test_attack2b_middle_verification_failed_but_executed_continues_honestly(
    monkeypatch: pytest.MonkeyPatch,
    fresh_server: Any,
) -> None:
    """Default mode: a MIDDLE item whose verification definitive-fails but which
    executed must not flush the batch, AND its follow_up_results entry must carry
    ok=False + the verification evidence (no silent swallowing)."""
    monkeypatch.delenv(QUEUE_ENV, raising=False)
    backend = ScriptedBackend(
        flip=False,
        active_window=WindowInfo(hwnd=1, pid=10, process_name="app.exe", title="App"),
    )
    session_id = _session(monkeypatch, backend)
    payload = await _run(
        session_id,
        {
            "action": "click",
            "x": 10,
            "y": 10,  # primary: no expectation -> uncertain -> continues
            "follow_ups": [
                {  # middle: deterministic window_state expectation that never
                    # appears -> definitive FAILED (RC-D11 update: a pixel-shaped
                    # stated effect would degrade to uncertain, which is not the
                    # failed-verdict-continues contract this attack pins)
                    "action": "keypress",
                    "keys": ["enter"],
                    "expected_effect": "open Calculator",
                },
                {"action": "keypress", "keys": ["enter"]},
            ],
            "include_screenshot_after": False,
        },
    )
    assert payload["follow_ups_stopped_reason"] is None, payload.get(
        "follow_ups_stopped_reason"
    )
    assert len(executed_summary(backend)) == 3, "the whole batch must dispatch"
    results = payload["follow_up_results"]
    assert [entry["action_type"] for entry in results] == ["click", "keypress", "keypress"]
    failed_entry = results[1]
    assert failed_entry["ok"] is False, failed_entry
    assert failed_entry["kind"] == "executed"
    assert failed_entry["verification_outcome"] == "failed", failed_entry
    assert failed_entry["verification_note"], "evidence must ride the entry"
    assert results[2]["index"] == 2  # the item AFTER the failed one still reported


# --- Attack 3: uncertain never upgrades ---------------------------------------------------------


async def test_attack3a_unflagged_abstaining_chain_ends_uncertain_not_verified(
    monkeypatch: pytest.MonkeyPatch,
    fresh_server: Any,
) -> None:
    """No expectation stated + a genuinely unchanged screen: every tier abstains or
    reports ambiguity -> UNCERTAIN. The result must still say NOT verified
    (ok=False) — never a free ok=true."""
    monkeypatch.delenv(QUEUE_ENV, raising=False)
    backend = ScriptedBackend(flip=False)
    session_id = _session(monkeypatch, backend)
    payload = await _run(
        session_id,
        {"action": "click", "x": 10, "y": 10, "include_screenshot_after": False},
    )
    assert payload["ok"] is False, "uncertain must not be reported as success"
    verification = payload["verification"]
    assert verification["outcome"] == "uncertain", verification
    assert verification["changed"] is False


async def test_attack3b_flagged_focus_intent_degrades_to_uncertain_not_failed(
    monkeypatch: pytest.MonkeyPatch,
    fresh_server: Any,
) -> None:
    """The W-1 degrade: a flagged focus-type click on a truly-unchanged screen ends
    UNCERTAIN (0.4), never definitive failed, and never verified."""
    monkeypatch.delenv(QUEUE_ENV, raising=False)
    backend = ScriptedBackend(flip=False)
    session_id = _session(monkeypatch, backend)
    payload = await _run(
        session_id,
        {
            "action": "click",
            "x": 10,
            "y": 10,
            "expected_effect": "Hex input focused",
            "include_screenshot_after": False,
        },
    )
    assert payload["ok"] is False, "the degraded verdict is still NOT a success"
    verification = payload["verification"]
    assert verification["outcome"] == "uncertain", verification
    assert abs(verification["confidence"] - 0.4) < 1e-9, verification
    assert "focus" in verification["note"].casefold(), verification


async def test_attack3c_all_uncertain_chain_completes_and_never_claims_verified(
    monkeypatch: pytest.MonkeyPatch,
    fresh_server: Any,
) -> None:
    """A 3-item batch whose every tier abstains: the queue completes (uncertain is
    not failed) and EVERY entry ends uncertain + ok=False — no upgrade anywhere."""
    monkeypatch.delenv(QUEUE_ENV, raising=False)
    backend = ScriptedBackend(flip=False)
    session_id = _session(monkeypatch, backend)
    payload = await _run(
        session_id,
        {
            "action": "click",
            "x": 10,
            "y": 10,  # unflagged uncertain (no expectation)
            "follow_ups": [
                {  # flagged focus-type -> uncertain 0.4
                    "action": "click",
                    "x": 20,
                    "y": 20,
                    "expected_effect": "Edit colors dialog opens",
                },
                {"action": "keypress", "keys": ["enter"]},  # unflagged uncertain
            ],
            "include_screenshot_after": False,
        },
    )
    assert payload["follow_ups_stopped_reason"] is None
    assert len(executed_summary(backend)) == 3
    results = payload["follow_up_results"]
    outcomes = [entry.get("verification_outcome") for entry in results]
    assert outcomes == ["uncertain", "uncertain", "uncertain"], outcomes
    assert all(entry["ok"] is False for entry in results), results
    assert "verified" not in outcomes


# --- Attack 4: env knob hygiene -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw_value", "expected"),
    [
        (None, "continue"),
        ("0", "continue"),
        ("garbage", "continue"),
        ("true", "continue"),
        ("1x", "continue"),
        ("1", "stop"),
        (" 1 ", "stop"),  # strip()-tolerant: padded "1" still enables strict
    ],
)
async def test_attack4_knob_values_map_exactly(
    monkeypatch: pytest.MonkeyPatch,
    fresh_server: Any,
    raw_value: str | None,
    expected: str,
) -> None:
    """Only the exact string "1" (whitespace-tolerant) restores the stop; unset,
    "0", and garbage continue. Read lazily per queue decision."""
    if raw_value is None:
        monkeypatch.delenv(QUEUE_ENV, raising=False)
    else:
        monkeypatch.setenv(QUEUE_ENV, raw_value)
    backend = ScriptedBackend(
        flip=False,
        active_window=WindowInfo(hwnd=1, pid=10, process_name="app.exe", title="App"),
    )
    session_id = _session(monkeypatch, backend)
    payload = await _run(
        session_id,
        {
            # RC-D11 (058): the failing item is a deterministic window_state
            # expectation that never appears -> definitive failed under strict;
            # a pixel-shaped stated effect would now degrade to uncertain.
            "action": "keypress",
            "keys": ["enter"],
            "expected_effect": "open Calculator",  # executes, fails honestly
            "follow_ups": [{"action": "type", "text": "r1"}],
            "include_screenshot_after": False,
        },
    )
    if expected == "stop":
        assert payload["follow_ups_stopped_reason"] == "verification_failed"
        assert len(executed_summary(backend)) == 1
    else:
        assert payload["follow_ups_stopped_reason"] is None, raw_value
        assert len(executed_summary(backend)) == 2, raw_value


async def test_attack4_knob_toggles_lazily_no_leak_between_batches(
    monkeypatch: pytest.MonkeyPatch,
    fresh_server: Any,
) -> None:
    """stop -> continue -> stop across THREE fresh batches in one test: the knob is
    re-read at each queue decision (no parse-time caching, no state leak)."""
    executed_counts: list[int] = []
    reasons: list[str | None] = []

    async def one_batch() -> None:
        backend = ScriptedBackend(
            flip=False,
            active_window=WindowInfo(hwnd=1, pid=10, process_name="app.exe", title="App"),
        )
        session_id = _session(monkeypatch, backend)
        payload = await _run(
            session_id,
            {
                # RC-D11 (058): deterministic window_state failure (never appears).
                "action": "keypress",
                "keys": ["enter"],
                "expected_effect": "open Calculator",
                "follow_ups": [{"action": "type", "text": "r1"}],
                "include_screenshot_after": False,
            },
        )
        reasons.append(payload["follow_ups_stopped_reason"])
        executed_counts.append(len(executed_summary(backend)))

    monkeypatch.setenv(QUEUE_ENV, "1")
    await one_batch()
    monkeypatch.delenv(QUEUE_ENV, raising=False)
    await one_batch()
    monkeypatch.setenv(QUEUE_ENV, "1")
    await one_batch()
    assert reasons == ["verification_failed", None, "verification_failed"], reasons
    assert executed_counts == [1, 2, 1], executed_counts


# --- Attack 5: message audit --------------------------------------------------------------------


async def test_attack5a_allowlist_rejection_names_the_real_gate(
    monkeypatch: pytest.MonkeyPatch,
    fresh_server: Any,
) -> None:
    """A process-allowlist rejection must name the validation/allowlist gate and the
    offending foreground process — and never carry the blanket
    "Grounding rejected." stamp."""
    backend = ScriptedBackend(
        flip=False,
        active_window=WindowInfo(hwnd=1, pid=4242, process_name="zcode.exe", title="driver console"),
    )
    session_id = _session(monkeypatch, backend, allowed_processes=["mspaint.exe"])
    payload = await _run(
        session_id, {"action": "click", "x": 10, "y": 10, "include_screenshot_after": False}
    )
    assert payload["ok"] is False
    message = str(payload["message"])
    assert message.startswith("Action rejected by validation:"), message
    assert "allowlist" in message.casefold(), message
    assert "zcode.exe" in message, "the offending FOREGROUND process must be named"
    assert "grounding rejected" not in message.casefold(), message
    assert payload["reasons"], "structured reasons still ride the rejection"
    assert backend.executed == []


class _DriftingForegroundBackend(ScriptedBackend):
    """Foreground identity changes AFTER the premise capture (hwnd+pid+process)."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(flip=False, **kwargs)
        self.set_active_window(
            WindowInfo(hwnd=1, pid=10, process_name="mspaint.exe", title="Untitled - Paint")
        )
        self.observe_calls = 0

    def observe(self) -> Any:
        self.observe_calls += 1
        if self.observe_calls >= 2:
            self.set_active_window(
                WindowInfo(hwnd=99, pid=77, process_name="explorer.exe", title="Desktop")
            )
        return super().observe()


async def test_attack5b_staleness_rejection_names_the_staleness_gate(
    monkeypatch: pytest.MonkeyPatch, fresh_server: Any
) -> None:
    """STALE_OBSERVATION whose re-grounding also fails must be reported as a
    STALENESS rejection ("Action rejected by staleness check:") — not the blanket
    "Grounding rejected." stamp."""
    monkeypatch.delenv(QUEUE_ENV, raising=False)
    backend = _DriftingForegroundBackend()
    session_id = _session(monkeypatch, backend)
    bundle = server._get_bundle(session_id)
    original_ground = bundle.agent._ground
    ground_calls = {"n": 0}

    def ground_then_refuse(action: Any, observation: Any) -> Any:
        ground_calls["n"] += 1
        if ground_calls["n"] >= 2:  # the P0-H re-ground attempt
            raise RuntimeError("r1 re-ground refusal")
        return original_ground(action, observation)

    monkeypatch.setattr(bundle.agent, "_ground", ground_then_refuse)
    payload = await _run(
        session_id, {"action": "click", "x": 10, "y": 10, "include_screenshot_after": False}
    )
    assert payload["ok"] is False
    message = str(payload["message"])
    assert message.startswith("Action rejected by staleness check:"), message
    assert "r1 re-ground refusal" in message
    assert "grounding rejected" not in message.casefold(), message
    assert backend.executed == []
    events = audit_events(bundle, session_id)
    phases = {
        (event.get("metadata") or {}).get("phase")
        for event in events
        if event["event_type"] == "observation"
    }
    assert "revalidate" in phases, "the single re-observe recovery ran before the refusal"


# --- Attack 6: H3 at HEAD — default policy launches server-side on NO_INSTANCE ------------------


async def test_attack6_default_policy_dispatches_server_side_launch(
    monkeypatch: pytest.MonkeyPatch,
    fresh_server: Any,
) -> None:
    """DEFAULT policy (no interference config, env unset): ensure_app on a
    NO_INSTANCE for an allowlisted target (mspaint) must reach the backend's
    launch dispatch (allow_launch=True) — the fake backend records the spawned
    needle exactly as LocalComputerBackend.ensure_app would Popen it
    (backend.py:3732-3743). The REM-D validator exemption must let the non-target
    foreground through."""
    monkeypatch.delenv(LAUNCH_ENV, raising=False)
    monkeypatch.delenv(QUEUE_ENV, raising=False)
    backend = _EnsureAppRecordingBackend(flip=False)  # app_windows empty -> NO_INSTANCE
    session_id = _session(monkeypatch, backend, allowed_processes=["mspaint.exe"])
    payload = await _run(session_id, {"action": "ensure_app", "target": "mspaint"})
    assert payload["ok"] is True, payload
    message = str(payload["message"])
    assert message.startswith("NO_INSTANCE"), message
    assert "launch=server" in message, message
    assert "launched=mspaint" in message, message
    assert backend.ensure_app_calls == ["mspaint"]
    assert backend.launched_processes == ["mspaint"], "the launch must DISPATCH"
    assert payload["verification"]["verification_method"] == "ensure_app_probe"


async def test_attack6b_driver_policy_restores_never_launch(
    monkeypatch: pytest.MonkeyPatch,
    fresh_server: Any,
) -> None:
    """CORTEX_ATTACH_OR_LAUNCH=driver (read at policy-parse time, i.e. before
    start_session) restores the never-launch default: NO_INSTANCE with launch=driver
    and NO recorded spawn."""
    monkeypatch.setenv(LAUNCH_ENV, "driver")
    backend = _EnsureAppRecordingBackend(flip=False)
    session_id = _session(monkeypatch, backend, allowed_processes=["mspaint.exe"])
    payload = await _run(session_id, {"action": "ensure_app", "target": "mspaint"})
    assert payload["ok"] is True
    message = str(payload["message"])
    assert message.startswith("NO_INSTANCE"), message
    assert "launch=driver" in message, message
    assert "launched=" not in message, message
    assert backend.launched_processes == [], "no spawn under the driver policy"
    monkeypatch.delenv(LAUNCH_ENV, raising=False)


# --- Attack 7: capture floor — the degrade adds ZERO captures -----------------------------------


@pytest.mark.parametrize("flagged", [False, True])
async def test_attack7_flagged_degrade_adds_no_extra_captures(
    monkeypatch: pytest.MonkeyPatch, fresh_server: Any, flagged: bool
) -> None:
    """A single click performs exactly 3 captures (direct_request + validate +
    post_action) whether the intent is focus-flagged or not: the W-1 uncertain
    degrade reuses the diff computed from the ALREADY-captured frames
    (verification.py:382-391,448-454) — no new capture anywhere."""
    monkeypatch.delenv(QUEUE_ENV, raising=False)
    backend = ScriptedBackend(flip=False)
    spec: dict[str, Any] = {
        "action": "click",
        "x": 10,
        "y": 10,
        "include_screenshot_after": False,
    }
    if flagged:
        spec["expected_effect"] = "Hex input focused"
    session_id = _session(monkeypatch, backend)
    await _run(session_id, spec)
    assert len(backend.observed) == 3, len(backend.observed)
    bundle = server._get_bundle(session_id)
    phases = [
        (event.get("metadata") or {}).get("phase")
        for event in audit_events(bundle, session_id)
        if event["event_type"] == "observation"
    ]
    assert phases == ["direct_request", "validate", "post_action"], phases
