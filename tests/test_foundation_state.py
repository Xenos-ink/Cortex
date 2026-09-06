"""Wave 1 foundation tests: state.py StopToken, TaskState, SessionRegistry."""

from __future__ import annotations

import threading
import time

import pytest

from computer_use_mcp.models import GroundedAction, TerminationReason
from computer_use_mcp.state import (
    ACTION_HISTORY_CAP,
    OBSERVATION_HISTORY_CAP,
    PLAN_NOTES_CAP,
    ActionRecord,
    SessionLimitExceeded,
    SessionRegistry,
    StopToken,
    TaskState,
    TaskStatus,
    TaskStopped,
)

# --- StopToken ---------------------------------------------------------------------------

def test_stop_token_initial_state() -> None:
    token = StopToken()
    assert token.stopped is False
    assert token.wait(0) is False


def test_stop_token_stop_is_idempotent() -> None:
    token = StopToken()
    token.stop()
    token.stop()
    token.stop()
    assert token.stopped is True


def test_stop_token_wait_returns_true_once_stopped() -> None:
    token = StopToken()

    def stop_later() -> None:
        time.sleep(0.02)
        token.stop()

    thread = threading.Thread(target=stop_later)
    thread.start()
    try:
        assert token.wait(timeout=2.0) is True
    finally:
        thread.join(timeout=2.0)
    assert token.stopped is True


def test_ensure_live_raises_task_stopped_after_stop() -> None:
    token = StopToken()
    token.ensure_live()  # live: no exception
    token.stop()
    with pytest.raises(TaskStopped):
        token.ensure_live()
    assert issubclass(TaskStopped, RuntimeError)


def test_stop_token_thread_stress() -> None:
    token = StopToken()
    errors: list[BaseException] = []

    def stopper() -> None:
        try:
            for _ in range(200):
                token.stop()
        except Exception as exc:  # noqa: BLE001 — stress workers must collect and report any failure
            errors.append(exc)

    def waiter() -> None:
        try:
            for _ in range(200):
                if token.stopped:
                    try:
                        token.ensure_live()
                    except TaskStopped:
                        pass
                    else:
                        errors.append(AssertionError("ensure_live did not raise"))
                else:
                    token.wait(0.001)
        except Exception as exc:  # noqa: BLE001 — stress workers must collect and report any failure
            errors.append(exc)

    threads = [threading.Thread(target=stopper)]
    threads += [threading.Thread(target=waiter) for _ in range(4)]
    for thread in threads:
        thread.start()
    token.stop()
    for thread in threads:
        thread.join(timeout=10.0)
    assert not any(thread.is_alive() for thread in threads)
    assert errors == []
    assert token.stopped is True
    with pytest.raises(TaskStopped):
        token.ensure_live()


# --- TaskState ---------------------------------------------------------------------------

def test_task_state_defaults() -> None:
    state = TaskState(goal="save the file")
    assert state.task_id
    assert state.goal == "save the file"
    assert state.subgoal is None
    assert state.status is TaskStatus.IDLE
    assert state.step_count == 0
    assert state.termination_reason is None
    assert state.started_at.tzinfo is not None
    assert state.model_call_count == 0
    assert state.recovery_attempts_task == 0
    assert state.recovery_attempts_action == 0
    assert len(state.action_history) == 0
    assert len(state.observation_history) == 0


def test_task_state_action_history_is_capped() -> None:
    state = TaskState()
    for _ in range(ACTION_HISTORY_CAP + 20):
        state.record_action(GroundedAction(action="click", point={"x": 1, "y": 2}))
    assert len(state.action_history) == ACTION_HISTORY_CAP


def test_task_state_observation_history_is_capped() -> None:
    state = TaskState()
    for index in range(OBSERVATION_HISTORY_CAP + 20):
        state.record_observation_id(f"obs-{index}")
    assert len(state.observation_history) == OBSERVATION_HISTORY_CAP
    assert list(state.observation_history)[-1] == f"obs-{OBSERVATION_HISTORY_CAP + 19}"


def test_task_state_plan_notes_are_capped() -> None:
    state = TaskState()
    for index in range(PLAN_NOTES_CAP + 5):
        state.add_plan_note(f"note-{index}")
    assert len(state.plan_notes) == PLAN_NOTES_CAP


def test_task_state_action_record_excludes_typed_text() -> None:
    state = TaskState()
    record = state.record_action(
        GroundedAction(action="type", text="password=hunter2secret", confidence=1.0)
    )
    assert isinstance(record, ActionRecord)
    assert record.action_type == "type"
    assert "hunter2secret" not in repr(record.model_dump())
    assert "hunter2secret" not in repr(state.model_dump())


def test_task_state_reset_action_scope() -> None:
    state = TaskState()
    state.recovery_attempts_action = 2
    state.reset_action_scope()
    assert state.recovery_attempts_action == 0
    assert state.recovery_attempts_task == 0  # task-level counter untouched


def test_task_state_terminate_maps_reason_to_status() -> None:
    completed = TaskState()
    completed.terminate(TerminationReason.COMPLETED)
    assert completed.status is TaskStatus.COMPLETED
    assert completed.termination_reason is TerminationReason.COMPLETED

    stopped = TaskState()
    stopped.terminate(TerminationReason.STOPPED_BY_USER)
    assert stopped.status is TaskStatus.STOPPED

    failed = TaskState()
    failed.terminate(TerminationReason.FAILED_VERIFICATION)
    assert failed.status is TaskStatus.FAILED


# --- SessionRegistry ---------------------------------------------------------------------

def test_registry_create_get_remove() -> None:
    registry = SessionRegistry(max_sessions=4)
    context = registry.create(goal="goal text")
    assert context.task.goal == "goal text"
    assert context.stop.stopped is False
    assert registry.get(context.session_id) is context
    assert registry.get("missing") is None
    assert len(registry) == 1
    removed = registry.remove(context.session_id)
    assert removed is context
    assert registry.remove(context.session_id) is None
    assert len(registry) == 0


def test_registry_named_session_and_duplicate_rejected() -> None:
    registry = SessionRegistry()
    registry.create(session_id="alpha")
    with pytest.raises(ValueError, match="already exists"):
        registry.create(session_id="alpha")


def test_registry_refuses_new_sessions_when_full() -> None:
    registry = SessionRegistry(max_sessions=2)
    registry.create()
    registry.create()
    with pytest.raises(SessionLimitExceeded) as excinfo:
        registry.create()
    assert excinfo.value.max_sessions == 2
    assert len(registry) == 2  # fail-closed: nothing evicted


def test_registry_max_sessions_must_be_positive() -> None:
    with pytest.raises(ValueError, match="max_sessions"):
        SessionRegistry(max_sessions=0)


def test_registry_thread_safety_enforces_cap() -> None:
    registry = SessionRegistry(max_sessions=3)
    succeeded: list[str] = []
    limit_errors = 0
    lock = threading.Lock()

    def worker() -> None:
        nonlocal limit_errors
        for _ in range(10):
            try:
                context = registry.create()
            except SessionLimitExceeded:
                with lock:
                    limit_errors += 1
            else:
                with lock:
                    succeeded.append(context.session_id)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10.0)
    assert len(succeeded) == 3
    assert limit_errors == 57
    assert len(registry) == 3
    assert len(registry.ids()) == 3
    assert len(set(registry.ids())) == 3  # session ids are unique
