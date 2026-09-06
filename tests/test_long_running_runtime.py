"""Long-Running Runtime integration tests (master-mission 003, Wave 3 / A5).

The runtime is tested directly against a minimal executor double (``FakeAgent``) and an
injectable monotonic clock — no sleeps, no threads, no real desktop. The REAL executor
path (``ComputerUseAgent`` + ``FakeComputerBackend`` + scripted provider) is covered by
``test_mcp_subtasks_tools.py``. Covered here:

- sequential execution order respects the dependency graph (deterministic ready-set);
- blocked propagation after a subtask failure; dead branches end UNRECOVERABLE;
- bounded replan: replacement work created outside the dead branch; attempts capped;
- shared session budget across subtasks (a subtask can NEVER reset session counters);
- per-subtask limit scope (fresh enforcer per subtask via the agent seam);
- the run-state step budget is capped to the REMAINING session step budget;
- approval epoch expiry stops execution fail-closed; epoch invalidated on goal change;
- unattended modifier (raise-only) blocks new subtasks; explicit approval renews;
- health UNSAFE stops execution and invalidates the epoch;
- checkpoint triggers: lifecycle (subtask completed / transition) and the periodic
  50-steps/30-minutes cadence with an injected clock;
- context summarization fires when due with a bounded request payload; a failing
  summarizer never breaks the loop (deterministic fallback);
- planner fail-closed without a key; manual create_subtask keeps working;
- one subtask execution at a time (RuntimeBusyError).
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from computer_use_mcp.audit import AuditLogger, Metrics
from computer_use_mcp.checkpoint_manager import CheckpointManager, CheckpointTrigger
from computer_use_mcp.context_manager import SummarizationRequest
from computer_use_mcp.health import ExpectedEnvironment, HealthMonitor, HealthProbes
from computer_use_mcp.limits import LimitEnforcer, Limits
from computer_use_mcp.long_running import (
    MAX_REPLAN_ATTEMPTS,
    LongRunningRuntime,
    PlannerUnavailableError,
    RuntimeBusyError,
    SubtaskNotRunnableError,
)
from computer_use_mcp.models import (
    ExecutionResult,
    GroundedAction,
    SessionState,
    SubtaskStatus,
    TerminationReason,
)
from computer_use_mcp.state import StopToken, TaskState

# --- fakes -------------------------------------------------------------------------------------


class FakeClock:
    """Injectable monotonic clock (no sleeps; deterministic advancement)."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeAgent:
    """Minimal executor double: ONE ``agent.run`` per subtask; records consumption.

    Consumption (actions / model calls) is recorded on the CURRENT enforcer exactly
    like the real controller records on ``self.enforcer``. ``terminations`` is an
    index-aligned list of termination reasons (last one repeats).
    """

    def __init__(
        self,
        *,
        steps_per_run: int = 1,
        terminations: list[TerminationReason] | None = None,
    ) -> None:
        self.task = TaskState()
        self.history: list[str] = []
        self.stop_token = StopToken()
        self.enforcer = LimitEnforcer(Limits(min_screenshot_interval_ms=0))
        self.enforcers: list[LimitEnforcer] = []
        self.calls: list[str] = []
        self.states: list[Any] = []
        self.approvals: list[Any] = []
        self.steps_per_run = steps_per_run
        self.terminations = list(terminations or [])

    def set_enforcer(self, enforcer: LimitEnforcer) -> None:
        self.enforcer = enforcer
        self.enforcers.append(enforcer)

    def _termination(self) -> TerminationReason:
        index = min(len(self.calls) - 1, len(self.terminations) - 1) if self.terminations else -1
        if index < 0:
            return TerminationReason.COMPLETED
        return self.terminations[index]

    async def run(self, goal: str, state: Any, approval: Any = None) -> list[ExecutionResult]:
        self.calls.append(goal)
        self.states.append(state)
        self.approvals.append(approval)
        self.enforcer.record_action()
        self.enforcer.record_action()
        self.enforcer.record_model_call()
        state.step_count += self.steps_per_run
        self.task.step_count += self.steps_per_run
        self.history.append(f"decision=done; goal={goal[:30]}")
        termination = self._termination()
        self.task.terminate(termination)
        return [
            ExecutionResult(
                ok=termination is TerminationReason.COMPLETED,
                action=GroundedAction(action="done"),
                message=f"ran: {goal[:50]}",
            )
        ]


class FakePlanner:
    """Scripted ``plan_subtasks`` provider double (untrusted-proposal producer)."""

    def __init__(self, proposals: list[Any] | None = None, error: Exception | None = None) -> None:
        self.proposals = list(proposals or [])
        self.error = error
        self.plan_calls = 0
        self.goals: list[str] = []

    async def plan_subtasks(self, goal: str, **kwargs: Any) -> Any:
        self.plan_calls += 1
        self.goals.append(goal)
        if self.error is not None:
            raise self.error
        if not self.proposals:
            raise RuntimeError("no scripted proposal")
        proposal = self.proposals[min(self.plan_calls - 1, len(self.proposals) - 1)]
        if isinstance(proposal, Exception):
            raise proposal
        return proposal


def _healthy_monitor(limits: Limits, clock: FakeClock) -> HealthMonitor:
    """A monitor whose probes always verify a stable expected environment."""
    probes = HealthProbes(
        observation=lambda: True,
        app=lambda: "app.exe",
        window=lambda: "App Window",
        hung=lambda: (),
        expectation=lambda: ExpectedEnvironment(active_app="app.exe", active_window="App Window"),
    )
    return HealthMonitor(limits, probes=probes, clock=clock)


def make_runtime(
    tmp_path: Any,
    *,
    agent: FakeAgent | None = None,
    provider: Any | None = None,
    clock: FakeClock | None = None,
    limits: Limits | None = None,
    goal: str = "long-running goal",
    require_approval: bool = True,
    summarizer: Any = None,
    health_monitor: HealthMonitor | None = None,
) -> tuple[LongRunningRuntime, FakeAgent, FakeClock]:
    clock = clock if clock is not None else FakeClock()
    limits = (limits if limits is not None else Limits(min_screenshot_interval_ms=0)).validate()
    agent = agent if agent is not None else FakeAgent()
    state = SessionState(
        session_id="sess-runtime",
        dry_run=False,
        require_approval=require_approval,
        max_steps=30,
    )
    runtime = LongRunningRuntime(
        session_id="sess-runtime",
        goal=goal,
        agent=agent,
        state=state,
        limits=limits,
        backend=None,
        auditor=AuditLogger(tmp_path / "audit"),
        metrics=Metrics(),
        checkpoint_manager=CheckpointManager(tmp_path / "checkpoints"),
        provider=provider,
        summarizer=summarizer,
        clock=clock,
        health_monitor=health_monitor if health_monitor is not None else _healthy_monitor(limits, clock),
    )
    return runtime, agent, clock


# --- sequential execution + dependencies --------------------------------------------------------


async def test_sequential_execution_respects_dependency_order(tmp_path: Any) -> None:
    runtime, agent, _clock = make_runtime(tmp_path)
    a = runtime.create_subtask("do A first")
    runtime.create_subtask("do B after A", [a.subtask_id])
    runtime.create_subtask("do C independent")
    outcome = await runtime.run_pending_subtasks()

    assert outcome.ok is True
    assert outcome.termination_reason == "completed"
    # Deterministic ready-set order: creation order among ready subtasks.
    assert agent.calls == ["do A first", "do B after A", "do C independent"]
    assert runtime.counts()["completed"] == 3
    # Every subtask ran through exactly ONE agent.run call (no second loop).
    assert len(agent.calls) == 3


async def test_failure_blocks_dependents_and_ends_unrecoverable_without_planner(tmp_path: Any) -> None:
    agent = FakeAgent(terminations=[TerminationReason.FAILED_VERIFICATION])
    runtime, _agent, _clock = make_runtime(tmp_path, agent=agent)
    a = runtime.create_subtask("doomed subtask")
    b = runtime.create_subtask("dependent subtask", [a.subtask_id])

    outcome = await runtime.run_pending_subtasks()

    assert outcome.ok is False
    assert outcome.termination_reason == "unrecoverable"
    assert runtime.counts()[SubtaskStatus.FAILED.value] == 1
    assert runtime.subtasks.require(b.subtask_id).status is SubtaskStatus.BLOCKED
    assert agent.calls == ["doomed subtask"]  # the dependent never executed
    # Bounded: exactly one replan attempt was made (no planner bound), then it stopped.
    assert outcome.replan_attempts == 1


async def test_progress_is_deterministic(tmp_path: Any) -> None:
    runtime, _agent, _clock = make_runtime(tmp_path)
    a = runtime.create_subtask("one")
    runtime.create_subtask("two", [a.subtask_id])
    runtime.create_subtask("three")
    runtime.create_subtask("four")

    progress = runtime.progress()
    assert progress["progress_percent"] == 0.0
    assert progress["total_subtasks"] == 4
    await runtime.run_single_subtask(a.subtask_id)
    progress = runtime.progress()
    assert progress["progress_percent"] == 25.0
    assert progress["completed_subtasks"] == 1
    assert progress["counts"]["pending"] == 3
    assert progress["current_subtask_id"] == a.subtask_id


# --- budgets -------------------------------------------------------------------------------------


async def test_session_budget_shared_across_subtasks_and_never_reset(tmp_path: Any) -> None:
    agent = FakeAgent(steps_per_run=2)  # each subtask consumes 2 steps + 2 actions + 1 call
    limits = Limits(min_screenshot_interval_ms=0, max_session_steps=3).validate()
    runtime, _agent, _clock = make_runtime(tmp_path, agent=agent, limits=limits)
    runtime.create_subtask("first")
    runtime.create_subtask("second")
    runtime.create_subtask("third")

    outcome = await runtime.run_pending_subtasks()

    # The shared tracker accumulated 4 steps across two subtasks (never reset), then the
    # boundary budget check refused the third subtask fail-closed.
    assert outcome.termination_reason == "limit_exceeded"
    assert outcome.ok is False
    snapshot = runtime.budget.snapshot()
    assert snapshot["steps"] == 4
    assert snapshot["actions"] == 4
    assert snapshot["model_calls"] == 2
    assert snapshot["subtasks"] == 2
    assert len(agent.calls) == 2


async def test_per_subtask_limits_are_a_fresh_scope(tmp_path: Any) -> None:
    agent = FakeAgent(steps_per_run=1)
    runtime, _agent, _clock = make_runtime(tmp_path, agent=agent)
    runtime.create_subtask("one")
    runtime.create_subtask("two")

    await runtime.run_pending_subtasks()

    assert len(agent.enforcers) == 2  # a fresh per-subtask enforcer per run
    assert agent.enforcers[0] is not agent.enforcers[1]
    for enforcer in agent.enforcers:
        assert enforcer.snapshot()["actions"] == 2  # this subtask's own consumption only


async def test_run_state_step_budget_capped_to_remaining_session_budget(tmp_path: Any) -> None:
    agent = FakeAgent(steps_per_run=1)
    limits = Limits(min_screenshot_interval_ms=0, max_session_steps=4).validate()
    runtime, _agent, _clock = make_runtime(tmp_path, agent=agent, limits=limits)
    for index in range(3):
        runtime.create_subtask(f"work {index}")

    outcome = await runtime.run_pending_subtasks()

    assert outcome.ok is True
    assert runtime.budget.snapshot()["steps"] == 3
    # Every run received a state whose max_steps loop bound was capped to
    # step_count + remaining session steps (tighter than the session's own 30).
    assert agent.states[0].max_steps == 4
    assert agent.states[1].max_steps == 4
    assert agent.states[2].max_steps == 4
    assert agent.states[0].max_steps < 30  # the session's own uncapped bound


# --- bounded replan ------------------------------------------------------------------------------


async def test_bounded_replan_replaces_dead_branch(tmp_path: Any) -> None:
    agent = FakeAgent(
        terminations=[TerminationReason.FAILED_VERIFICATION, TerminationReason.COMPLETED]
    )
    planner = FakePlanner(
        proposals=[
            {
                "subtasks": [
                    {"subtask_id": "fix", "description": "repair the failed step", "depends_on": []},
                    {"subtask_id": "redo", "description": "redo the dependent work", "depends_on": ["fix"]},
                ]
            }
        ]
    )
    runtime, _agent, _clock = make_runtime(tmp_path, agent=agent, provider=planner)
    a = runtime.create_subtask("doomed subtask")
    b = runtime.create_subtask("dependent subtask", [a.subtask_id])

    outcome = await runtime.run_pending_subtasks()

    assert outcome.ok is True
    assert outcome.termination_reason == "completed"
    assert planner.plan_calls == 1  # one bounded replan sufficed
    assert agent.calls[0] == "doomed subtask"
    assert agent.calls[1:] == ["repair the failed step", "redo the dependent work"]
    # The dead-branch dependent stays blocked; replacement work completed the goal.
    assert runtime.subtasks.require(b.subtask_id).status is SubtaskStatus.BLOCKED
    assert runtime.counts()["completed"] == 2


async def test_bounded_replan_attempts_are_capped(tmp_path: Any) -> None:
    agent = FakeAgent(terminations=[TerminationReason.FAILED_VERIFICATION])
    counter = {"n": 0}

    class UniqueFailingPlanner:
        """Each replan proposes NEW work that then fails; the cap must end the loop."""

        def __init__(self) -> None:
            self.plan_calls = 0

        async def plan_subtasks(self, goal: str, **kwargs: Any) -> dict[str, Any]:
            self.plan_calls += 1
            counter["n"] += 1
            return {
                "subtasks": [
                    {
                        "subtask_id": f"fix{counter['n']}",
                        "description": f"failing replacement {counter['n']}",
                        "depends_on": [],
                    }
                ]
            }

    planner = UniqueFailingPlanner()
    runtime, _agent, _clock = make_runtime(tmp_path, agent=agent, provider=planner)
    a = runtime.create_subtask("doomed subtask")
    runtime.create_subtask("dependent subtask", [a.subtask_id])

    outcome = await runtime.run_pending_subtasks()

    assert outcome.termination_reason == "unrecoverable"
    assert planner.plan_calls == MAX_REPLAN_ATTEMPTS
    assert outcome.replan_attempts == MAX_REPLAN_ATTEMPTS
    assert len(agent.calls) == 1 + MAX_REPLAN_ATTEMPTS


async def test_replan_never_depends_on_dead_ids(tmp_path: Any) -> None:
    agent = FakeAgent(terminations=[TerminationReason.FAILED_VERIFICATION])
    planner = FakePlanner(
        proposals=[
            {
                "subtasks": [
                    {
                        "subtask_id": "behind_dead",
                        "description": "work behind the failed subtask",
                        "depends_on": ["doomed-subtask-id"],
                    }
                ]
            }
        ]
    )
    runtime, _agent, _clock = make_runtime(tmp_path, agent=agent, provider=planner)
    a = runtime.create_subtask("doomed subtask", subtask_id="doomed-subtask-id")
    runtime.create_subtask("dependent subtask", [a.subtask_id])

    outcome = await runtime.run_pending_subtasks()

    assert outcome.termination_reason == "unrecoverable"
    assert planner.plan_calls == 1  # the filtered proposal produced no work
    assert outcome.replan_attempts == 1
    assert "behind_dead" not in {sid for sid in runtime.subtasks.ids()}


# --- approval epochs + unattended modifier --------------------------------------------------------


async def test_approval_epoch_expiry_stops_execution_fail_closed(tmp_path: Any) -> None:
    clock = FakeClock()
    limits = Limits(min_screenshot_interval_ms=0, approval_epoch_seconds=60.0).validate()
    runtime, agent, clock = make_runtime(tmp_path, clock=clock, limits=limits)
    runtime.create_subtask("some work")

    clock.advance(61.0)  # the 30-minute-style epoch (clamped floor 60s) has expired
    outcome = await runtime.run_pending_subtasks()

    assert outcome.ok is False
    assert outcome.requires_approval is True
    assert outcome.termination_reason == "approval_exhausted"
    assert agent.calls == []  # nothing executed after expiry (fail closed)
    # A fresh explicit approval renews the epoch and execution proceeds.
    renewed = await runtime.run_pending_subtasks(approve_next_action=True)
    assert renewed.ok is True
    assert agent.calls == ["some work"]


async def test_epoch_invalidated_on_goal_change(tmp_path: Any) -> None:
    runtime, agent, _clock = make_runtime(tmp_path)
    runtime.create_subtask("some work")

    runtime.set_goal("a materially different goal")

    outcome = await runtime.run_pending_subtasks()
    assert outcome.requires_approval is True
    assert agent.calls == []


async def test_unattended_modifier_blocks_new_subtasks_until_fresh_approval(tmp_path: Any) -> None:
    clock = FakeClock()
    runtime, agent, clock = make_runtime(
        tmp_path, clock=clock, require_approval=False  # full-scope epoch
    )
    runtime.create_subtask("unattended work")

    clock.advance(3601.0)  # over one hour without human interaction
    outcome = await runtime.run_pending_subtasks()

    assert outcome.requires_approval is True
    assert agent.calls == []
    # Raise-only policy: explicit fresh approval restarts the unattended clock.
    renewed = await runtime.run_pending_subtasks(approve_next_action=True)
    assert renewed.ok is True
    assert agent.calls == ["unattended work"]


async def test_full_scope_epoch_still_expires(tmp_path: Any) -> None:
    clock = FakeClock()
    limits = Limits(min_screenshot_interval_ms=0, approval_epoch_seconds=60.0).validate()
    runtime, agent, clock = make_runtime(
        tmp_path, clock=clock, limits=limits, require_approval=False
    )
    runtime.create_subtask("work")

    clock.advance(61.0)
    outcome = await runtime.run_pending_subtasks()

    # require_approval=False is NOT an unlimited pass: the full-scope epoch expires too.
    assert outcome.requires_approval is True
    assert agent.calls == []


# --- health boundary ------------------------------------------------------------------------------


async def test_health_unsafe_stops_execution_and_invalidates_epoch(tmp_path: Any) -> None:
    clock = FakeClock()
    limits = Limits(min_screenshot_interval_ms=0).validate()
    probes = HealthProbes(
        observation=lambda: True,
        app=lambda: "other.exe",  # unexpected foreground application
        window=lambda: "App Window",
        hung=lambda: (),
        expectation=lambda: ExpectedEnvironment(active_app="app.exe", active_window="App Window"),
    )
    monitor = HealthMonitor(limits, probes=probes, clock=clock)
    runtime, agent, _clock = make_runtime(
        tmp_path, clock=clock, limits=limits, health_monitor=monitor
    )
    runtime.create_subtask("work in the wrong app is refused")

    outcome = await runtime.run_pending_subtasks()

    assert outcome.termination_reason == "blocked_safety"
    assert outcome.health_verdict == "unsafe"
    assert agent.calls == []  # no unsafe action was executed
    assert runtime.epochs.current_epoch().invalidated is True


# --- checkpoints ----------------------------------------------------------------------------------


async def test_lifecycle_checkpoints_fire_on_subtask_completion(tmp_path: Any) -> None:
    runtime, _agent, _clock = make_runtime(tmp_path)
    a = runtime.create_subtask("checkpointed work")

    assert runtime.checkpoint_status()["has_checkpoint"] is False
    outcome = await runtime.run_single_subtask(a.subtask_id)

    assert outcome.ok is True
    status = runtime.checkpoint_status()
    assert status["has_checkpoint"] is True
    assert status["last_trigger"] == CheckpointTrigger.SUBTASK_COMPLETED.value
    assert status["last_checkpoint_at"] is not None


async def test_periodic_checkpoint_cadence_uses_injected_clock(tmp_path: Any) -> None:
    clock = FakeClock()
    runtime, _agent, clock = make_runtime(tmp_path, clock=clock)
    a = runtime.create_subtask("work")

    assert runtime.checkpoint_due() is False
    await runtime.run_single_subtask(a.subtask_id)  # lifecycle checkpoint re-anchors
    assert runtime.checkpoint_due() is False

    clock.advance(1801.0)  # past the 30-minute cadence
    assert runtime.checkpoint_due() is True
    path = runtime.checkpoint(CheckpointTrigger.PERIODIC)
    assert runtime.checkpoint_status()["last_trigger"] == CheckpointTrigger.PERIODIC.value
    assert runtime.checkpoint_status()["has_checkpoint"] is True
    assert path.name == "checkpoint.json"


async def test_session_level_run_writes_before_session_end_checkpoint(tmp_path: Any) -> None:
    runtime, _agent, _clock = make_runtime(tmp_path)
    runtime.create_subtask("only work")

    outcome = await runtime.run_pending_subtasks()

    assert outcome.ok is True
    assert runtime.checkpoint_status()["last_trigger"] == CheckpointTrigger.BEFORE_SESSION_END.value


# --- context summarization ------------------------------------------------------------------------


async def test_context_summarization_fires_with_bounded_payload(tmp_path: Any) -> None:
    requests: list[SummarizationRequest] = []

    def summarizer(request: SummarizationRequest) -> dict[str, Any]:
        requests.append(request)
        return {"notes": "compact summary of progress", "current_task": "summarized task"}

    agent = FakeAgent(steps_per_run=1)
    limits = Limits(min_screenshot_interval_ms=0, context_summarize_every=1).validate()
    runtime, _agent, _clock = make_runtime(
        tmp_path, agent=agent, limits=limits, summarizer=summarizer
    )
    for index in range(12):
        runtime.create_subtask(f"work {index}")

    outcome = await runtime.run_pending_subtasks()

    assert outcome.ok is True
    assert requests, "the provider-backed summarizer was never consulted"
    for request in requests:
        assert len(request.recent_history) <= 10  # bounded recent window, never full history
        assert len(request.plan_notes) <= 50
    payload = runtime.context.build_request_payload()
    assert payload["summary"]["notes"] == "compact summary of progress"
    # The executor's cross-run history was folded into the bounded context window:
    # only the CURRENT run's entries remain after the last fold.
    assert len(agent.history) <= 1


async def test_failing_summarizer_never_breaks_the_loop(tmp_path: Any) -> None:
    def summarizer(request: SummarizationRequest) -> Any:
        raise RuntimeError("summarizer backend down")

    agent = FakeAgent(steps_per_run=1)
    limits = Limits(min_screenshot_interval_ms=0, context_summarize_every=1).validate()
    runtime, _agent, _clock = make_runtime(
        tmp_path, agent=agent, limits=limits, summarizer=summarizer
    )
    runtime.create_subtask("work despite broken summarizer")

    outcome = await runtime.run_pending_subtasks()

    assert outcome.ok is True  # deterministic fallback used; loop unaffected
    assert runtime.context.build_request_payload()["summary"]["current_goal"]


# --- planner fail-closed degradation ----------------------------------------------------------------


async def test_planner_fail_closed_without_key_manual_creation_still_works(tmp_path: Any) -> None:
    from computer_use_mcp.provider import ProviderError

    planner = FakePlanner(error=ProviderError("vision provider not configured"))
    runtime, _agent, _clock = make_runtime(tmp_path, provider=planner)

    with pytest.raises(PlannerUnavailableError):
        await runtime.plan_from_llm("some goal")

    # Typed fail-closed degradation: the session stays usable for manual creation.
    subtask = runtime.create_subtask("manual fallback subtask")
    assert subtask.status is SubtaskStatus.PENDING
    outcome = await runtime.run_pending_subtasks(approve_next_action=True)
    assert outcome.ok is True


async def test_planner_proposal_is_validated_before_creation(tmp_path: Any) -> None:
    from computer_use_mcp.plan_validator import PlanRejectedError

    planner = FakePlanner(
        proposals=[
            {
                "subtasks": [
                    {"subtask_id": "a", "description": "valid", "depends_on": []},
                    {"subtask_id": "a", "description": "duplicate id", "depends_on": []},
                ]
            }
        ]
    )
    runtime, _agent, _clock = make_runtime(tmp_path, provider=planner)

    with pytest.raises(PlanRejectedError) as excinfo:
        await runtime.plan_from_llm("goal")
    assert "duplicate_subtask_id" in excinfo.value.codes
    assert len(runtime.subtasks) == 0  # nothing was created from the rejected plan


# --- single-subtask execution + exclusivity ---------------------------------------------------------


async def test_run_single_subtask_enforces_state_rules(tmp_path: Any) -> None:
    runtime, _agent, _clock = make_runtime(tmp_path)
    a = runtime.create_subtask("first")
    b = runtime.create_subtask("second", [a.subtask_id])

    with pytest.raises(Exception) as not_ready:  # SubtaskNotReadyError
        await runtime.run_single_subtask(b.subtask_id)
    assert "dependencies" in str(not_ready.value)

    outcome = await runtime.run_single_subtask(a.subtask_id)
    assert outcome.ok is True
    assert runtime.subtasks.require(a.subtask_id).status is SubtaskStatus.COMPLETED

    with pytest.raises(SubtaskNotRunnableError):
        await runtime.run_single_subtask(a.subtask_id)  # terminal; cannot re-run

    outcome = await runtime.run_single_subtask(b.subtask_id)
    assert outcome.ok is True


async def test_runtime_busy_error_on_concurrent_execution(tmp_path: Any) -> None:
    gate = asyncio.Event()
    started = asyncio.Event()

    class GatedAgent(FakeAgent):
        async def run(self, goal: str, state: Any, approval: Any = None) -> list[ExecutionResult]:
            started.set()
            await gate.wait()
            return await super().run(goal, state, approval)

    agent = GatedAgent()
    runtime, _agent, _clock = make_runtime(tmp_path, agent=agent)
    a = runtime.create_subtask("gated work")

    first = asyncio.ensure_future(runtime.run_single_subtask(a.subtask_id))
    await started.wait()
    with pytest.raises(RuntimeBusyError):
        await runtime.run_single_subtask(a.subtask_id)
    gate.set()
    outcome = await first
    assert outcome.ok is True


async def test_unknown_subtask_fail_closed(tmp_path: Any) -> None:
    runtime, _agent, _clock = make_runtime(tmp_path)
    from computer_use_mcp.subtask_manager import UnknownSubtaskError

    with pytest.raises(UnknownSubtaskError):
        await runtime.run_single_subtask("missing-id")
