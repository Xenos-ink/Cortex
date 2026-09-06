"""Checkpoint integrity seal + state cross-checks (remediation of the zeroed-budget
resume refill finding) and the tool-boundary type gates.

Covers, each with a POSITIVE (legitimate values pass) and NEGATIVE (tampered value ->
typed fail-closed refusal) case:

- Integrity seal roundtrip: every written checkpoint carries an HMAC-SHA256 seal over
  the tamper-sensitive fields (budget counters, limits, subtask states, identity
  anchors); an untampered file loads and resumes with counters EXACT.
- Seal enforcement on load: any edit of a covered field, a stripped seal, a malformed
  seal, a missing per-installation key, or a replaced key refuses the file with
  ``CheckpointValidationError`` (fail closed; the file is never deleted or repaired).
- Deterministic counter-ceiling cross-checks: ``budget.subtasks <= limits.max_subtasks``
  and ``budget.steps <= limits.max_session_steps`` — legitimate checkpoints pass,
  counter forgeries beyond the checkpoint's own ceilings are refused (defense in depth
  behind the seal). Lower-bound checks are deliberately absent: a provider "done"
  decision completes a subtask while consuming ZERO steps/actions, so such bounds would
  reject legitimate checkpoints (see the seal for zeroed-counter defense).
- Runtime defense in depth: a SEALED checkpoint with absurd-but-not-provably-impossible
  counters (actions/model_calls = 1e9, no deterministic ceiling) resumes with counters
  EXACT and every further run fails closed with ``limit_exceeded`` — inflating counters
  grants no more work.
- Tool boundary: ``create_subtask`` with a non-iterable (or string) ``depends_on``
  returns the typed ``invalid_subtask`` error code instead of escaping a TypeError.
- Dry-run budget semantics (doc note): dry-run executions consume a model call per
  decision but advance NO step/action counters (the executor short-circuit precedes
  ``step_count += 1``); this is why the checkpoint cross-checks use no lower bounds.

Threat model (honest scope): the seal defends against out-of-band tampering of the
checkpoint FILE alone. An attacker who can also read/replace the per-installation key
file under the checkpoint base dir (same-user/full-disk access) can re-seal forged
state and is OUT OF SCOPE — the OS user boundary is the control for that adversary.

Run with the repo suite: ``.venv/Scripts/python.exe -m pytest tests/ -q``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from test_checkpoint_manager import build_state, make_limits
from test_controller_integration import (
    FAST_LIMITS,
    ScriptedBackend,
    ScriptedProvider,
    make_session,
)

from computer_use_mcp import server
from computer_use_mcp.checkpoint_manager import (
    INTEGRITY_KEY_FILENAME,
    SEAL_ALGORITHM,
    CheckpointManager,
    CheckpointValidationError,
)
from computer_use_mcp.limits import Limits
from computer_use_mcp.models import AgentDecision, GroundedAction
from computer_use_mcp.resume_manager import ResumeManager
from computer_use_mcp.state import SessionRegistry


@pytest.fixture
def fresh_server(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Any:
    """Per-test server isolation incl. the shared checkpoint store (same base+key)."""
    monkeypatch.setenv("COMPUTER_USE_MCP_LOG_DIR", str(tmp_path / "audit"))
    monkeypatch.setattr(server, "_registry", SessionRegistry(max_sessions=8))
    monkeypatch.setattr(server, "_bundles", {})
    monkeypatch.setattr(server, "_stopped_sessions", {})
    checkpoint_manager = CheckpointManager(tmp_path / "checkpoints")
    monkeypatch.setattr(server, "_checkpoint_manager", checkpoint_manager)
    monkeypatch.setattr(server, "_resume_manager", ResumeManager(checkpoint_manager))
    return server


def _tamper(path: Any, mutate: Any) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    mutate(data)
    path.write_text(json.dumps(data), encoding="utf-8")
    return data


_MINIMAL_CONTEXT = {
    "snapshot_version": 1,
    "goal": "g",
    "current_task": "",
    "app_window_state": "",
    "summary": None,
    "recent_history": [],
    "plan_notes": [],
    "categories": {},
    "steps": 0,
    "steps_at_last_summary": 0,
}


def _budget(actions: int = 0, model_calls: int = 0, steps: int = 0, subtasks: int = 0) -> dict[str, Any]:
    return {
        "snapshot_version": 1,
        "elapsed_seconds": 0.0,
        "actions": actions,
        "model_calls": model_calls,
        "steps": steps,
        "subtasks": subtasks,
    }


# --- seal roundtrip (positive case) + determinism ---------------------------------------------


def test_sealed_checkpoint_roundtrip_with_exact_counters(tmp_path: Path) -> None:
    manager, kwargs, state = build_state(tmp_path)
    path = manager.write_checkpoint(**kwargs)
    data = json.loads(path.read_text(encoding="utf-8"))
    seal = data["integrity"]
    assert seal["algorithm"] == SEAL_ALGORITHM == "HMAC-SHA256"
    assert len(seal["digest"]) == 64 and seal["digest"] == seal["digest"].lower()
    payload = manager.load(path)  # legitimate values PASS the seal + cross-checks
    for key in ("actions", "model_calls", "steps", "subtasks"):
        assert payload.budget[key] == state["tracker"].snapshot()[key]
    # The sealed budget counters are exactly the tracker's (steps=7 <= 100 cap,
    # subtasks=1 <= 50 cap): positive case for both ceiling cross-checks.
    assert payload.budget["steps"] == 7 and payload.budget["subtasks"] == 1


def test_seal_is_deterministic_identical_writes_are_byte_identical(tmp_path: Path) -> None:
    manager, kwargs, _ = build_state(tmp_path)
    kwargs = {**kwargs, "created_at": datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)}
    first = manager.write_checkpoint(**kwargs)
    second = manager.write_checkpoint(**kwargs)
    assert first.read_bytes() == second.read_bytes()


def test_resume_from_sealed_checkpoint_succeeds_with_counters_exact(tmp_path: Path) -> None:
    manager, kwargs, _state = build_state(tmp_path)
    path = manager.write_checkpoint(**kwargs)
    bundle = ResumeManager(manager).prepare(path)
    restored = bundle.budget.snapshot()
    checkpointed = manager.load(path).budget
    for key in ("actions", "model_calls", "steps", "subtasks"):
        assert restored[key] == checkpointed[key]
    assert checkpointed["actions"] == 3 and checkpointed["steps"] == 7


# --- seal enforcement: every covered-field tamper is refused, file intact ---------------------


@pytest.mark.parametrize(
    ("label", "mutate"),
    [
        ("budget_actions", lambda d: d["budget"].__setitem__("actions", 0)),
        ("budget_steps", lambda d: d["budget"].__setitem__("steps", 0)),
        ("budget_elapsed", lambda d: d["budget"].__setitem__("elapsed_seconds", 0)),
        ("budget_subtasks", lambda d: d["budget"].__setitem__("subtasks", 0)),
        ("limits_actions", lambda d: d["limits"].__setitem__("max_session_actions", 2000)),
        ("subtask_status", lambda d: d["subtasks"]["subtasks"][0].__setitem__("status", "pending")),
        ("subtask_result_count", lambda d: d["subtasks"]["subtasks"][0]["results"].pop()),
        ("session_id", lambda d: d.__setitem__("session_id", "sess-impersonator")),
        ("goal", lambda d: d.__setitem__("goal", "a different goal entirely")),
        (
            "current_subtask_id",
            lambda d: d.__setitem__("current_subtask_id", d["subtasks"]["subtasks"][2]["subtask_id"]),
        ),
    ],
)
def test_any_covered_field_edit_breaks_the_seal_and_is_refused(
    tmp_path: Path, label: str, mutate: Any
) -> None:
    del label  # parametrization id only
    manager, kwargs, _ = build_state(tmp_path)
    path = manager.write_checkpoint(**kwargs)
    _tamper(path, mutate)
    with pytest.raises(CheckpointValidationError, match="seal"):
        manager.load(path)
    assert path.exists()  # corrupt files are refused, never deleted or repaired


def test_stripping_or_forging_the_seal_is_refused(tmp_path: Path) -> None:
    manager, kwargs, _ = build_state(tmp_path)
    path = manager.write_checkpoint(**kwargs)
    pristine = path.read_text(encoding="utf-8")

    path.write_text(pristine, encoding="utf-8")
    _tamper(path, lambda d: d.pop("integrity"))
    with pytest.raises(CheckpointValidationError, match="no integrity seal"):
        manager.load(path)

    path.write_text(pristine, encoding="utf-8")
    _tamper(path, lambda d: d["integrity"].__setitem__("algorithm", "MD5"))
    with pytest.raises(CheckpointValidationError, match="seal"):
        manager.load(path)

    path.write_text(pristine, encoding="utf-8")
    _tamper(path, lambda d: d["integrity"].__setitem__("digest", "ff" * 32))
    with pytest.raises(CheckpointValidationError, match="seal"):
        manager.load(path)

    path.write_text(pristine, encoding="utf-8")
    _tamper(path, lambda d: d.__setitem__("integrity", {"algorithm": SEAL_ALGORITHM}))
    with pytest.raises(CheckpointValidationError, match="seal"):
        manager.load(path)


def test_seal_covers_exactly_the_tamper_sensitive_fields(tmp_path: Path) -> None:
    """A wall-clock stamp (``created_at``) is outside the seal: editing it does not
    affect resume semantics (elapsed budget lives in the sealed ``budget`` block), so
    the load path stays conservative and does not refuse it."""
    manager, kwargs, _ = build_state(tmp_path)
    path = manager.write_checkpoint(**kwargs)
    _tamper(path, lambda d: d.__setitem__("created_at", "2026-01-02T03:04:05Z"))
    assert manager.load(path).created_at is not None  # accepted, resume semantics intact


def test_missing_or_replaced_key_file_refuses_the_load(tmp_path: Path) -> None:
    manager, kwargs, _ = build_state(tmp_path)
    path = manager.write_checkpoint(**kwargs)
    key_path = tmp_path / INTEGRITY_KEY_FILENAME
    assert key_path.exists()  # the key lives under the base dir, outside session dirs

    key_path.unlink()
    with pytest.raises(CheckpointValidationError, match="integrity key"):
        manager.load(path)  # unauthenticatable -> fail closed, file left intact

    key_path.write_text("ab" * 32, encoding="ascii")  # out-of-band key replacement
    with pytest.raises(CheckpointValidationError, match="seal"):
        manager.load(path)
    assert path.exists()


# --- deterministic counter-ceiling cross-checks (defense in depth behind the seal) ------------


def test_ceiling_cross_check_refuses_subtasks_counter_beyond_the_checkpointed_limit(
    tmp_path: Path,
) -> None:
    """NEGATIVE: ``budget.subtasks`` can never exceed the checkpoint's own
    ``max_subtasks`` (each start is gated by ``check_subtasks`` before ``record_subtask``);
    a sealed-but-forged counter past the ceiling is refused at load."""
    manager = CheckpointManager(base_dir=tmp_path)
    limits = make_limits()  # max_subtasks = 50
    payload = manager.capture_payload(
        session_id="sess-ceiling-subtasks",
        goal="forged subtasks counter",
        subtasks_snapshot={"subtasks": []},
        budget_snapshot=_budget(subtasks=51),
        limits=limits,
        context_snapshot=_MINIMAL_CONTEXT,
    )
    path = manager.write(payload)  # write+seal succeed (cross-checks are load-time)
    with pytest.raises(CheckpointValidationError, match="max_subtasks"):
        manager.load(path)


def test_ceiling_cross_check_refuses_steps_counter_beyond_the_checkpointed_limit(
    tmp_path: Path,
) -> None:
    """NEGATIVE: cumulative steps can never exceed the checkpoint's own
    ``max_session_steps`` (each run's step delta is capped to the remaining budget);
    a forged counter past the ceiling is refused at load."""
    manager = CheckpointManager(base_dir=tmp_path)
    limits = make_limits()  # max_session_steps = 100
    payload = manager.capture_payload(
        session_id="sess-ceiling-steps",
        goal="forged steps counter",
        subtasks_snapshot={"subtasks": []},
        budget_snapshot=_budget(steps=101),
        limits=limits,
        context_snapshot=_MINIMAL_CONTEXT,
    )
    path = manager.write(payload)
    with pytest.raises(CheckpointValidationError, match="max_session_steps"):
        manager.load(path)


def test_ceiling_cross_check_accepts_counters_within_the_ceiling(tmp_path: Path) -> None:
    """POSITIVE: counters at (not past) the checkpoint's own ceilings pass — the bounds
    are exact, never heuristic."""
    manager = CheckpointManager(base_dir=tmp_path)
    limits = make_limits(max_session_steps=7, max_subtasks=1)
    payload = manager.capture_payload(
        session_id="sess-at-ceiling",
        goal="counters exactly at the ceilings",
        subtasks_snapshot={"subtasks": []},
        budget_snapshot=_budget(steps=7, subtasks=1),
        limits=limits,
        context_snapshot=_MINIMAL_CONTEXT,
    )
    path = manager.write(payload)
    payload_loaded = manager.load(path)
    assert payload_loaded.budget["steps"] == 7 and payload_loaded.budget["subtasks"] == 1


# --- runtime defense in depth: sealed overshoot counters still grant no work -------------------


async def test_sealed_overshoot_counters_resume_exactly_then_fail_closed_at_run_time(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """actions/model_calls have NO deterministic ceiling (a run may legitimately
    overshoot the session block mid-run), so a SEALED checkpoint can carry 1e9 — and it
    must resume with counters EXACT while every further run fails closed with the typed
    ``limit_exceeded`` error (inflated counters are DoS-only, never a bypass)."""
    manager = server._checkpoint_manager
    limits = Limits(**FAST_LIMITS).validate()
    payload = manager.capture_payload(
        session_id="sess-sealed-overshoot",
        goal="absurd but sealed counters",
        subtasks_snapshot={"subtasks": []},
        budget_snapshot=_budget(actions=10**9, model_calls=10**9),
        limits=limits,
        context_snapshot=_MINIMAL_CONTEXT,
    )
    path = manager.write(payload)

    # Hermetic resume: inject fake backend/provider factories — otherwise start_session
    # would construct a REAL LocalComputerBackend and consume the process-once per-
    # monitor DPI awareness the session-scoped real_backend fixture depends on.
    monkeypatch.setattr(server, "_backend_factory", ScriptedBackend)
    monkeypatch.setattr(server, "_provider_factory", lambda: ScriptedProvider([]))
    response = server.start_session(limits=FAST_LIMITS, resume_from_checkpoint=str(path))
    assert "error" not in response, response
    new_session_id = str(response["session_id"])
    counters = server.get_session_progress(new_session_id)["resource_counters"]
    assert counters["actions"] == 10**9 and counters["model_calls"] == 10**9

    created = server.create_subtask(session_id=new_session_id, description="any work")
    assert created["ok"] is True
    attempt = await server.run_subtask(
        session_id=new_session_id, subtask_id=created["subtask"]["subtask_id"]
    )
    assert attempt["ok"] is False
    assert attempt["error"] == "limit_exceeded"


# --- tool boundary: non-iterable depends_on is a typed error, never a TypeError ----------------


def test_create_subtask_rejects_non_iterable_and_string_depends_on(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id, _bundle, _backend, _ = make_session(
        monkeypatch, provider=ScriptedProvider([]), limits=FAST_LIMITS
    )
    for hostile in (42, 3.5, True, {"a": 1}, "not-a-list", b"bytes"):
        response = server.create_subtask(
            session_id=session_id, description="work", depends_on=hostile
        )
        assert response["ok"] is False, (hostile, response)
        assert response["error"] == "invalid_subtask", (hostile, response)
    assert server.list_subtasks(session_id)["total"] == 0  # nothing was created


def test_create_subtask_still_accepts_valid_dependency_lists(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id, _bundle, _backend, _ = make_session(
        monkeypatch, provider=ScriptedProvider([]), limits=FAST_LIMITS
    )
    first = server.create_subtask(session_id=session_id, description="stage one")
    assert first["ok"] is True
    second = server.create_subtask(
        session_id=session_id,
        description="stage two",
        depends_on=[first["subtask"]["subtask_id"]],
    )
    assert second["ok"] is True
    assert second["subtask"]["depends_on"] == [first["subtask"]["subtask_id"]]


# --- dry-run budget semantics (doc note pinned as behavior) -------------------------------------


async def test_dry_run_consumes_model_calls_but_no_step_or_action_counters(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DOC NOTE (executor semantics, unchanged): the dry-run EXECUTE short-circuit
    returns a stub result BEFORE ``enforcer.record_action()`` and ``step_count += 1``,
    while every DECIDE still consumes a model call. A completed dry-run subtask
    therefore shows model_calls >= 1 with steps == 0 and actions == 0 — consumption is
    honestly attributed per phase, and this is exactly why the checkpoint integrity
    cross-checks use no lower bounds on steps/actions (they are not implied)."""
    provider = ScriptedProvider(
        [
            AgentDecision(
                status="action",
                action=GroundedAction(action="click", point={"x": 1, "y": 1}, confidence=1.0),
            ),
            AgentDecision(status="done", summary="done"),
        ]
    )
    session_id, _bundle, _backend, _ = make_session(
        monkeypatch, provider=provider, limits=FAST_LIMITS, require_approval=False
    )
    created = server.create_subtask(session_id=session_id, description="dry-run work")
    outcome = await server.run_subtask(
        session_id=session_id, subtask_id=created["subtask"]["subtask_id"]
    )
    assert outcome["ok"] is True
    assert outcome["status"] == "completed"
    counters = server.get_session_progress(session_id)["resource_counters"]
    assert counters["steps"] == 0
    assert counters["actions"] == 0
    assert counters["model_calls"] >= 2  # one per DECIDE (click + done)
