"""RED-TEAM adversarial tests (Wave 4 validator, A7) — attack groups C (checkpoint/
resume tampering) and F (unbounded-growth attacks).

Additive ONLY. A passing test means the attack was BLOCKED (fail-closed); where an
attack SUCCEEDS the test pins the vulnerable behavior with an explicit
``RED-TEAM FINDING`` marker so the report can route it as NOT FULLY FIXED.

C. Checkpoint tampering: corrupted JSON, unknown/newer schema versions, missing fields,
   wrong types, injected duplicate ids / dependency cycles / unknown deps, planted
   screenshot payloads, over-cap result counts, self-inconsistent subtask counts,
   enlarged limits (canonical-limits equality gate), oversized payloads (size cap on
   BOTH write and load), planted secrets (redaction before persist + contains_secret
   write refusal with the previous checkpoint left intact), environment mismatch on
   resume, unknown checkpoint paths, and counter tampering (zeroed counters — CLOSED:
   the HMAC integrity seal refuses the tampered file fail-closed; inflated counters —
   fail closed at LOAD via the seal + counter-ceiling cross-checks, with the runtime
   budget gates kept as defense in depth).
F. Growth attacks: bounded history window, bounded/truncated plan notes, capped subtask
   results with heavy payloads stripped, summarize trigger at the configured cadence,
   bounded fallback summaries, checkpoint size cap on REAL oversized state, and bounded
   MCP list/progress responses with worst-case (50 x 2000-char) content.

Run with the repo suite: ``.venv/Scripts/python.exe -m pytest tests/ -q``.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from test_controller_integration import (
    FAST_LIMITS,
    ScriptedBackend,
    ScriptedProvider,
    make_session,
)

from computer_use_mcp import server
from computer_use_mcp.checkpoint_manager import (
    MAX_CHECKPOINT_BYTES,
    CheckpointError,
    CheckpointManager,
    CheckpointRedactionError,
    CheckpointValidationError,
    CheckpointWriteError,
)
from computer_use_mcp.context_manager import ContextManager
from computer_use_mcp.limits import Limits
from computer_use_mcp.models import (
    SUBTASK_RESULTS_CAP,
    AgentDecision,
    ExecutionResult,
    GroundedAction,
    WindowInfo,
)
from computer_use_mcp.resume_manager import ResumeManager, ResumeRefusalError
from computer_use_mcp.state import SessionRegistry
from computer_use_mcp.subtask_manager import SubtaskManager


@pytest.fixture
def fresh_server(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Any:
    """Per-test server isolation incl. the shared checkpoint store."""
    monkeypatch.setenv("COMPUTER_USE_MCP_LOG_DIR", str(tmp_path / "audit"))
    monkeypatch.setattr(server, "_registry", SessionRegistry(max_sessions=8))
    monkeypatch.setattr(server, "_bundles", {})
    monkeypatch.setattr(server, "_stopped_sessions", {})
    checkpoint_manager = CheckpointManager(tmp_path / "checkpoints")
    monkeypatch.setattr(server, "_checkpoint_manager", checkpoint_manager)
    monkeypatch.setattr(server, "_resume_manager", ResumeManager(checkpoint_manager))
    return server


def _done_provider() -> ScriptedProvider:
    return ScriptedProvider([AgentDecision(status="done", summary="done")])


def _click_decision(x: int = 50) -> AgentDecision:
    return AgentDecision(
        status="action",
        action=GroundedAction(action="click", point={"x": x, "y": 50}, confidence=1.0),
    )


def _manager(tmp_path: Any) -> CheckpointManager:
    return CheckpointManager(tmp_path / "checkpoints")


def _tamper(path: Any, mutate: Any) -> dict[str, Any]:
    """Load a checkpoint file, apply a mutation, write it back, return the new data."""
    data = json.loads(path.read_text(encoding="utf-8"))
    mutate(data)
    path.write_text(json.dumps(data), encoding="utf-8")
    return data


def _set_dotted(data: dict[str, Any], dotted: str, value: Any) -> None:
    parent = data
    parts = dotted.split(".")
    for part in parts[:-1]:
        parent = parent[part]
    parent[parts[-1]] = value


async def _checkpointed_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> tuple[Any, Any, dict[str, Any], str]:
    """A stopped session with real progress: one completed + one pending subtask and
    NONZERO mirrored counters (a click executed inside the subtask on the real
    non-dry-run execute path). The default fake backend records no foreground, so the
    checkpoint's environment expectations are empty (resume re-verification passes).

    Returns (session_id, checkpoint_path, progress_before, pristine_json)."""
    provider = ScriptedProvider(
        [
            AgentDecision(status="done", summary="done"),  # run_goal consumes this
            _click_decision(50),  # the subtask executes this click (mirrored counter)
            AgentDecision(status="done", summary="done"),
        ]
    )
    session_id, _bundle, _backend, _ = make_session(
        monkeypatch, provider=provider, dry_run=False, require_approval=False,
        limits=FAST_LIMITS,
    )
    await server.run_goal(session_id, "tamper target goal")  # consumes the first click
    first = server.create_subtask(session_id=session_id, description="stage one")
    server.create_subtask(
        session_id=session_id,
        description="stage two",
        depends_on=[first["subtask"]["subtask_id"]],
    )
    await server.run_subtask(session_id=session_id, subtask_id=first["subtask"]["subtask_id"])
    progress = server.get_session_progress(session_id)
    assert progress["resource_counters"]["actions"] >= 1  # mirrored subtask consumption
    server.stop_session(session_id)
    path = _manager(tmp_path).checkpoint_path(session_id)
    assert path.exists()
    return session_id, path, progress, path.read_text(encoding="utf-8")


# ==============================================================================================
# GROUP C — checkpoint file tampering (every structural/integrity defect must be rejected)
# ==============================================================================================


def test_group_c_load_rejects_corrupted_json(tmp_path: Any) -> None:
    manager = CheckpointManager(tmp_path / "checkpoints")
    truncated = tmp_path / "corrupt.json"
    truncated.write_text('{"schema_version": 1, "session_id": "x"', encoding="utf-8")
    with pytest.raises(CheckpointValidationError):
        manager.load(truncated)
    wrong_type = tmp_path / "array.json"
    wrong_type.write_text("[]", encoding="utf-8")  # valid JSON, wrong top-level type
    with pytest.raises(CheckpointValidationError):
        manager.load(wrong_type)


async def test_group_c_load_rejects_unknown_newer_or_missing_schema_version(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    _session_id, path, _progress, _pristine = await _checkpointed_session(
        monkeypatch, tmp_path
    )
    manager = _manager(tmp_path)
    for version in (2, 999, "1", None, True, 1.0):
        _tamper(path, lambda data, v=version: data.__setitem__("schema_version", v))
        with pytest.raises(CheckpointValidationError):
            manager.load(path)
    _tamper(path, lambda data: data.pop("schema_version"))
    with pytest.raises(CheckpointValidationError):
        manager.load(path)


async def test_group_c_load_rejects_missing_fields_and_wrong_types(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    _session_id, path, _progress, pristine = await _checkpointed_session(
        monkeypatch, tmp_path
    )
    manager = _manager(tmp_path)

    for key in ("budget", "subtasks", "limits", "context", "goal", "session_id"):
        path.write_text(pristine, encoding="utf-8")  # reset between mutations
        _tamper(path, lambda data, k=key: data.pop(k))
        with pytest.raises(CheckpointValidationError):
            manager.load(path)

    mutations: list[tuple[str, Any]] = [
        ("budget.actions", "many"),
        ("budget.steps", -3),
        ("budget.snapshot_version", "one"),
        ("limits.max_session_steps", "500"),
        ("goal", 123),
        ("session_id", ""),
        ("current_subtask_id", "ghost-subtask"),
        ("trigger", "NOT_A_TRIGGER"),
        ("created_at", "not-a-date"),
    ]
    for dotted, value in mutations:
        path.write_text(pristine, encoding="utf-8")  # reset between mutations
        _tamper(path, lambda data, k=dotted, v=value: _set_dotted(data, k, v))
        with pytest.raises(CheckpointValidationError):
            manager.load(path)


async def test_group_c_load_rejects_injected_duplicate_ids_cycles_and_unknown_deps(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    _session_id, path, _progress, pristine = await _checkpointed_session(
        monkeypatch, tmp_path
    )
    manager = _manager(tmp_path)

    def duplicate(data: dict[str, Any]) -> None:
        entries = data["subtasks"]["subtasks"]
        entries.append(dict(entries[0]))

    path.write_text(pristine, encoding="utf-8")
    _tamper(path, duplicate)
    with pytest.raises(CheckpointValidationError):
        manager.load(path)

    def cycle(data: dict[str, Any]) -> None:
        entries = data["subtasks"]["subtasks"]
        entries[0]["depends_on"] = [entries[1]["subtask_id"]]

    path.write_text(pristine, encoding="utf-8")
    _tamper(path, cycle)
    with pytest.raises(CheckpointValidationError):
        manager.load(path)

    def unknown_dep(data: dict[str, Any]) -> None:
        entries = data["subtasks"]["subtasks"]
        entries[1]["depends_on"] = ["ghost-subtask"]

    path.write_text(pristine, encoding="utf-8")
    _tamper(path, unknown_dep)
    with pytest.raises(CheckpointValidationError):
        manager.load(path)

    def plant_screenshot(data: dict[str, Any]) -> None:
        entries = data["subtasks"]["subtasks"]
        entries[0]["results"][0]["screenshot_after_base64"] = "QUFB"

    path.write_text(pristine, encoding="utf-8")
    _tamper(path, plant_screenshot)
    with pytest.raises(CheckpointValidationError):
        manager.load(path)

    def flood_results(data: dict[str, Any]) -> None:
        entries = data["subtasks"]["subtasks"]
        base = dict(entries[0]["results"][0])
        entries[0]["results"] = [base for _ in range(SUBTASK_RESULTS_CAP + 1)]

    path.write_text(pristine, encoding="utf-8")
    _tamper(path, flood_results)
    with pytest.raises(CheckpointValidationError):
        manager.load(path)


async def test_group_c_load_rejects_self_inconsistent_subtask_count_vs_limits(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """A state that violates its own checkpointed limits is corrupt -> refused."""
    _session_id, path, _progress, pristine = await _checkpointed_session(
        monkeypatch, tmp_path
    )
    manager = _manager(tmp_path)

    def add_and_shrink(data: dict[str, Any]) -> None:
        entries = data["subtasks"]["subtasks"]
        clone = dict(entries[0])
        clone["subtask_id"] = "extra-subtask"
        entries.append(clone)
        data["limits"]["max_subtasks"] = 2  # canonical value, now violated by the count

    _tamper_from(path, pristine, add_and_shrink)
    with pytest.raises(CheckpointValidationError):
        manager.load(path)


def _tamper_from(path: Any, pristine: str, mutate: Any) -> None:
    path.write_text(pristine, encoding="utf-8")
    _tamper(path, mutate)


async def test_group_c_load_rejects_enlarged_limits_canonical_equality_gate(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """Any attempt to enlarge the budget via the file is refused: the persisted limits
    must equal the CURRENT clamping mechanism's output exactly."""
    _session_id, path, _progress, pristine = await _checkpointed_session(
        monkeypatch, tmp_path
    )
    manager = _manager(tmp_path)
    for field, value in (
        ("max_session_actions", 999_999),
        ("max_session_model_calls", 999_999),
        ("max_session_steps", 99_999),
        ("max_subtasks", 500),
        ("max_session_seconds", 999_999.0),
        ("approval_epoch_seconds", 1.0),
        ("max_task_seconds", 0.0),
        ("max_actions", 100_000),
        ("context_summarize_every", 0),
        ("health_check_interval", 1.0),
    ):
        _tamper_from(
            path, pristine, lambda data, k=field, v=value: data["limits"].__setitem__(k, v)
        )
        with pytest.raises(CheckpointValidationError):
            manager.load(path)


async def test_group_c_inflated_counters_in_file_are_fail_closed_by_budget_checks(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """Inflating counters in the file grants no more work: the tampered file is refused
    at LOAD (integrity seal mismatch, plus the counter-ceiling cross-checks as defense
    in depth), so resume is refused fail-closed. The runtime budget gates remain as a
    second layer for counters without a deterministic ceiling — covered by the sealed
    overshoot test in ``test_checkpoint_integrity.py``."""
    _session_id, path, _progress, pristine = await _checkpointed_session(
        monkeypatch, tmp_path
    )

    def inflate(data: dict[str, Any]) -> None:
        data["budget"]["actions"] = 10**9
        data["budget"]["steps"] = 10**9

    _tamper_from(path, pristine, inflate)
    manager = _manager(tmp_path)
    with pytest.raises(CheckpointValidationError):
        manager.load(path)
    response = server.start_session(limits=FAST_LIMITS, resume_from_checkpoint=str(path))
    assert response["ok"] is False
    assert response["error"] == "invalid_checkpoint"
    assert not server._bundles  # no session was left behind by the refusal


async def test_group_c_zeroed_counters_in_file_are_refused_by_integrity_seal(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """CLOSED RED-TEAM FINDING (former vector C, counter tampering — was MEDIUM).

    The attack: stop a session with real progress, edit the checkpoint file setting
    every ``budget`` counter to 0, and resume — the old validator accepted it (only
    non-negativity was checked) and the fresh tracker max-merged 0 over 0, REFILLING
    the session budget (spec section 8 violation). Now every checkpoint carries an
    HMAC-SHA256 integrity seal over the tamper-sensitive fields; the zeroing edit
    invalidates the seal, so the file is refused at load and resume is refused with the
    typed ``invalid_checkpoint`` error — the pending subtask never runs at full budget.
    """
    _session_id, path, progress_before, pristine = await _checkpointed_session(
        monkeypatch, tmp_path
    )
    assert progress_before["resource_counters"]["actions"] >= 1

    def zero(data: dict[str, Any]) -> None:
        data["budget"]["actions"] = 0
        data["budget"]["model_calls"] = 0
        data["budget"]["steps"] = 0
        data["budget"]["subtasks"] = 0
        data["budget"]["elapsed_seconds"] = 0

    _tamper_from(path, pristine, zero)
    manager = _manager(tmp_path)
    with pytest.raises(CheckpointValidationError, match="seal"):
        manager.load(path)  # the tampered file is REJECTED (fail-closed)

    response = server.start_session(limits=FAST_LIMITS, resume_from_checkpoint=str(path))
    assert response["ok"] is False
    assert response["error"] == "invalid_checkpoint"
    assert not server._bundles  # nothing was restored, no session was left behind
    assert path.exists()  # the corrupt file is never deleted or repaired


async def test_group_c_resume_rejects_environment_mismatch_and_missing_environment(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """Resume re-verifies the CURRENT environment; mismatch/absence -> typed refusal."""
    paint = WindowInfo(hwnd=1, pid=10, process_name="mspaint.exe", title="Untitled - Paint")
    calc = WindowInfo(hwnd=2, pid=20, process_name="calc.exe", title="Calculator")
    session_id, _bundle, _backend, _ = make_session(
        monkeypatch,
        provider=_done_provider(),
        backend=ScriptedBackend(active_window=paint, windows=[paint]),
        limits=FAST_LIMITS,
    )
    created = server.create_subtask(session_id=session_id, description="stage one")
    await server.run_subtask(session_id=session_id, subtask_id=created["subtask"]["subtask_id"])
    server.stop_session(session_id)
    path = _manager(tmp_path).checkpoint_path(session_id)

    resume_manager = ResumeManager(_manager(tmp_path))
    # Different foreground process -> refusal.
    with pytest.raises(ResumeRefusalError):
        resume_manager.prepare(
            path,
            current_environment={
                "active_process_name": "calc.exe",
                "active_window_title": "Calculator",
            },
        )
    # Missing current identity (stale-state attempt) -> refusal, never a pass.
    with pytest.raises(ResumeRefusalError):
        resume_manager.prepare(path, current_environment={})
    # The matching environment continues.
    bundle = resume_manager.prepare(
        path,
        current_environment={
            "active_process_name": "mspaint.exe",
            "active_window_title": "Untitled - Paint",
        },
    )
    assert bundle.checks.ok is True

    # Server path: each fresh session re-verifies its backend's CURRENT environment.
    monkeypatch.setattr(server, "_backend_factory", lambda: ScriptedBackend())
    no_identity = server.start_session(limits=FAST_LIMITS, resume_from_checkpoint=str(path))
    assert no_identity["ok"] is False
    assert no_identity["error"] == "resume_refused"

    monkeypatch.setattr(
        server,
        "_backend_factory",
        lambda: ScriptedBackend(active_window=calc, windows=[calc]),
    )
    mismatch = server.start_session(limits=FAST_LIMITS, resume_from_checkpoint=str(path))
    assert mismatch["ok"] is False
    assert mismatch["error"] == "resume_refused"

    monkeypatch.setattr(
        server,
        "_backend_factory",
        lambda: ScriptedBackend(active_window=paint, windows=[paint]),
    )
    matching = server.start_session(limits=FAST_LIMITS, resume_from_checkpoint=str(path))
    assert "error" not in matching, matching
    assert matching["resumed"] is True


async def test_group_c_resume_with_unknown_path_and_re_run_of_stopped_session(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    session_id, _bundle, _backend, _ = make_session(
        monkeypatch, provider=_done_provider(), limits=FAST_LIMITS
    )
    server.create_subtask(session_id=session_id, description="stage one")
    server.stop_session(session_id)

    # Unknown / missing checkpoint path -> typed refusal, no session left behind.
    missing = tmp_path / "nowhere" / "checkpoint.json"
    response = server.start_session(limits=FAST_LIMITS, resume_from_checkpoint=str(missing))
    assert response["ok"] is False
    assert response["error"] == "invalid_checkpoint"
    assert not server._bundles

    # Resuming the checkpoint as a continuation works (that is the design), but the
    # OLD session id stays stopped, and the resumed session can be stopped again —
    # after which every tool refuses (fail-closed stopped-session policy).
    path = _manager(tmp_path).checkpoint_path(session_id)
    resumed = server.start_session(limits=FAST_LIMITS, resume_from_checkpoint=str(path))
    assert "error" not in resumed, resumed
    new_session_id = str(resumed["session_id"])
    old_id_attempt = await server.run_subtask(session_id=session_id, subtask_id="whatever")
    assert old_id_attempt["ok"] is False
    assert old_id_attempt["error"] == "session_stopped"
    server.stop_session(new_session_id)
    after = await server.run_subtask(session_id=new_session_id, subtask_id="whatever")
    assert after["ok"] is False
    assert after["error"] == "session_stopped"


async def test_group_c_resume_is_continuation_counters_equal_never_refilled(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """Baseline: an UNTAMPERED checkpoint restores counters EXACTLY (continuation)."""
    _session_id, path, progress_before, _pristine = await _checkpointed_session(
        monkeypatch, tmp_path
    )
    assert progress_before["resource_counters"]["actions"] >= 1
    response = server.start_session(limits=FAST_LIMITS, resume_from_checkpoint=str(path))
    assert "error" not in response, response
    progress_after = server.get_session_progress(str(response["session_id"]))
    assert progress_after["resource_counters"] == progress_before["resource_counters"]
    assert progress_after["elapsed_seconds"] >= progress_before["elapsed_seconds"]


async def test_group_c_oversized_checkpoints_refused_on_write_and_load(
    tmp_path: Any,
) -> None:
    """The size cap is enforced on BOTH paths; nothing unbounded ever persists."""
    tiny = CheckpointManager(tmp_path / "tiny", max_checkpoint_bytes=64)
    payload = tiny.capture_payload(
        session_id="size-cap-probe",
        goal="write a checkpoint bigger than 64 bytes",
        subtasks_snapshot={"subtasks": []},
        budget_snapshot={
            "snapshot_version": 1,
            "elapsed_seconds": 0.0,
            "actions": 0,
            "model_calls": 0,
            "steps": 0,
            "subtasks": 0,
        },
        limits=Limits().validate(),
        context_snapshot={
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
        },
    )
    with pytest.raises(CheckpointWriteError):
        tiny.write(payload)
    assert not tiny.has_checkpoint("size-cap-probe")

    # Load side: a file over the cap is refused before parsing.
    big = tmp_path / "big" / "checkpoint.json"
    big.parent.mkdir(parents=True)
    big.write_text("x" * 256, encoding="utf-8")
    with pytest.raises(CheckpointValidationError):
        CheckpointManager(tmp_path / "tiny", max_checkpoint_bytes=64).load(big)
    assert MAX_CHECKPOINT_BYTES == 8 * 1024 * 1024  # the default cap is 8 MiB


async def test_group_c_planted_secrets_are_redacted_and_never_return_on_load(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """A secret planted in subtask state is redacted BEFORE persist; the file on disk
    never carries the raw secret and no secret material returns on load."""
    session_id, _bundle, _backend, _ = make_session(
        monkeypatch, provider=_done_provider(), limits=FAST_LIMITS
    )
    created = server.create_subtask(
        session_id=session_id,
        description="deploy with key AKIAIOSFODNN7EXAMPLE right away",
    )
    await server.run_subtask(session_id=session_id, subtask_id=created["subtask"]["subtask_id"])
    server.stop_session(session_id)
    path = _manager(tmp_path).checkpoint_path(session_id)

    raw = path.read_text(encoding="utf-8")
    assert "AKIAIOSFODNN7EXAMPLE" not in raw, "raw secret persisted to disk!"
    assert "[REDACTED:" in raw  # the redaction placeholder is what was stored

    payload = _manager(tmp_path).load(path)
    descriptions = [entry["description"] for entry in payload.subtasks["subtasks"]]
    assert any("[REDACTED:" in text for text in descriptions)
    assert all("AKIAIOSFODNN7EXAMPLE" not in text for text in descriptions)


async def test_group_c_secret_gate_refuses_the_write_previous_checkpoint_intact(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """When redaction cannot clean the payload, the contains_secret gate refuses the
    write BEFORE any filesystem mutation; the PREVIOUS valid checkpoint stays intact."""
    session_id, _bundle, _backend, _ = make_session(
        monkeypatch, provider=_done_provider(), limits=FAST_LIMITS
    )
    created = server.create_subtask(session_id=session_id, description="stage one")
    await server.run_subtask(session_id=session_id, subtask_id=created["subtask"]["subtask_id"])
    server.stop_session(session_id)
    path = _manager(tmp_path).checkpoint_path(session_id)
    intact_before = path.read_text(encoding="utf-8")

    import computer_use_mcp.checkpoint_manager as checkpoint_module

    runtime = server._bundles.get(session_id)
    assert runtime is None  # stopped sessions keep no live bundle (fail-closed hygiene)

    # Probe the gate on the real write path with a fresh manager + forced gate.
    manager = CheckpointManager(tmp_path / "gate")
    monkeypatch.setattr(checkpoint_module, "contains_secret", lambda text: True)
    payload = manager.capture_payload(
        session_id="gate-probe",
        goal="innocuous goal",
        subtasks_snapshot={"subtasks": []},
        budget_snapshot={
            "snapshot_version": 1,
            "elapsed_seconds": 0.0,
            "actions": 0,
            "model_calls": 0,
            "steps": 0,
            "subtasks": 0,
        },
        limits=Limits().validate(),
        context_snapshot={
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
        },
    )
    with pytest.raises(CheckpointRedactionError):
        manager.write(payload)
    assert not manager.has_checkpoint("gate-probe")  # nothing was written
    # The refused write left the previous checkpoint byte-identical.
    assert path.read_text(encoding="utf-8") == intact_before


# ==============================================================================================
# GROUP F — growth attacks (no unbounded structures anywhere)
# ==============================================================================================


def test_group_f_history_window_is_bounded() -> None:
    context = ContextManager(goal="growth probe")
    for index in range(5000):
        context.append_history(f"history entry {index} with padding {'y' * 2000}")
    assert len(context.recent_history) <= 10  # hard window (5..10), never grows
    payload = context.build_request_payload()
    assert len(payload["recent_history"]) <= 10


def test_group_f_plan_notes_are_bounded_and_truncated() -> None:
    context = ContextManager(goal="notes probe")
    for index in range(500):
        context.add_plan_note(f"note {index}: {'huge ' * 2000}")
    notes = context.plan_notes
    assert len(notes) <= 50
    assert all(len(note) <= 500 for note in notes)
    payload = context.build_request_payload()
    assert len(payload["plan_notes"]) <= 50


def test_group_f_subtask_results_capped_and_heavy_payloads_stripped() -> None:
    manager = SubtaskManager()
    created = manager.create("results probe")
    manager.start(created.subtask_id)
    for index in range(100):
        manager.record_result(
            created.subtask_id,
            ExecutionResult(
                ok=True,
                action=GroundedAction(action="done"),
                message=f"result {index}",
                screenshot_after_base64="QUFB" * 1000,  # heavy payload attempt
            ),
        )
    stored = manager.require(created.subtask_id)
    assert len(stored.results) == 20  # retention cap
    assert all(item.screenshot_after_base64 is None for item in stored.results)


def test_group_f_summarize_trigger_fires_at_the_configured_cadence() -> None:
    context = ContextManager(goal="cadence probe", summarize_every=5)
    fired = [context.record_step() for _ in range(12)]
    # Steps 1-4: not due; step 5 crosses the threshold; the trigger STAYS due until a
    # summarization actually resets the anchor (every later step still reports due).
    assert fired[:4] == [False, False, False, False]
    assert fired[4] is True
    assert all(fired[4:])


async def test_group_f_summarization_failure_falls_back_to_bounded_summary() -> None:
    async def failing_summarizer(request: Any) -> Any:
        raise RuntimeError("summarizer down (red-team)")

    context = ContextManager(
        goal="fallback probe", summarizer=failing_summarizer, summarize_every=1
    )
    for _ in range(3):
        context.record_step()
    summary = await context.summarize()  # never raises
    assert summary.current_goal == "fallback probe"
    assert len(summary.notes) <= 4000  # bounded fallback, never unbounded growth


async def test_group_f_checkpoint_of_oversized_state_is_refused(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REAL oversized state (results carrying huge messages) cannot produce an unbounded
    checkpoint: the write is refused by the size cap (fail-closed, bounded disk)."""
    provider = _done_provider()
    _session_id, bundle, _backend, _ = make_session(
        monkeypatch, provider=provider, limits=FAST_LIMITS
    )
    runtime = server._get_or_create_runtime(bundle)
    manager = runtime.subtasks
    ids = []
    for index in range(5):
        created = manager.create(f"bulk {index}")
        manager.start(created.subtask_id)
        ids.append(created.subtask_id)
    filler = "m" * 100_000
    for subtask_id in ids:
        for _position in range(20):
            manager.record_result(
                subtask_id,
                ExecutionResult(
                    ok=True, action=GroundedAction(action="done"), message=filler
                ),
            )
    # 5 x 20 x 100KB ~= 10MB of results > the 8 MiB checkpoint cap -> refused.
    with pytest.raises(CheckpointError) as excinfo:
        runtime.checkpoint()
    assert isinstance(excinfo.value, CheckpointWriteError)


async def test_group_f_mcp_responses_are_bounded_with_worst_case_content(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """list_subtasks / get_session_progress stay bounded with 50 subtasks carrying
    maximum-length descriptions (no unbounded MCP payloads)."""
    provider = _done_provider()
    session_id, _bundle, _backend, _ = make_session(
        monkeypatch, provider=provider, limits=FAST_LIMITS
    )
    await server.run_goal(session_id, "g" * 2000)  # maximum-length goal
    for index in range(50):
        description = (f"work {index} " + "d" * 1990)[:2000]
        response = server.create_subtask(session_id=session_id, description=description)
        assert response["ok"] is True

    listed = server.list_subtasks(session_id)
    assert listed["total"] == 50
    encoded = json.dumps(listed)
    assert len(encoded) < 1_000_000, f"unbounded list_subtasks payload: {len(encoded)}"
    for summary in listed["subtasks"]:
        assert len(summary["description"]) <= 2000
        assert set(summary) == {
            "subtask_id",
            "description",
            "status",
            "depends_on",
            "created_at",
            "started_at",
            "completed_at",
            "result_count",
            "recovery_attempts",
            "failure",
        }

    progress = server.get_session_progress(session_id)
    progress_encoded = json.dumps(progress)
    assert len(progress_encoded) < 50_000, (
        f"unbounded progress payload: {len(progress_encoded)}"
    )
    assert len(progress["goal"]) <= 2000
    assert set(progress["resource_counters"]) == {
        "actions",
        "model_calls",
        "steps",
        "subtasks",
    }
