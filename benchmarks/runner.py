"""Benchmark runner for computer-use-mcp (harness only — NO score claims).

Executes task definitions (``tasks/*.yaml``) through the server tool surface
(``start_session`` → ``computer_execute`` → ``stop_session``). RETARGETED (run_goal
removal): the internal decide/recovery loop is gone, so the runner now plays the host —
it resolves each scripted provider step to a grounded action and issues it as ONE
``computer_execute`` call, observing the verification outcome per action. Two modes:

- ``--mode fake`` (default): every task runs against :class:`FakeWorldBackend`
  (see ``fakeworld.py``) — the full direct pipeline (observation → grounding →
  validation → risk → execution → re-observe → verification) executes for real; only
  the desktop is simulated. No GUI required.
- ``--mode env``: tasks run against real applications on THIS Windows box (Notepad,
  classic Calculator, Edge on a local page) via :mod:`appwin` lifecycles.

A real vision-model provider plugs in later by replacing :class:`DirectCallDriver`'s
resolution stage; the per-task metrics (grounding, verification, safety, actions/task,
latencies) are collected identically either way. Output: ``results/<run_id>.json`` plus
a printed summary table. Every artifact carries the disclaimer: HARNESS VALIDATION,
NOT SCORES.

Verification doctrine for task authors: the runtime's default strategy chain is used
per ``computer_execute`` call, so provider steps should state ``expected_effect``
values the default chain can decide (``visual_change`` screenshots, ``window_state``
title needles on keypress/focus_window steps); ``expected_text`` and ``predicate``
hints require perception strategies that only exist in the E2E suite. The task-level
``verification`` predicate (evaluated by the runner against real window text / files /
display values) carries the semantic check in every mode.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# --- bootstrap: BOTH invocation forms must work (F4) ---------------------------------------
# ``python -m benchmarks.runner``  → __package__ == "benchmarks"; cwd (repo root) is on
# sys.path, everything below resolves normally.
# ``python benchmarks/runner.py`` → __package__ is None/""; the PACKAGE PARENT (repo
# root) is inserted on sys.path so the absolute ``benchmarks.*`` imports below resolve
# (sys.path[0] would otherwise be the benchmarks/ directory itself). The conditional
# ``src`` insertion covers the runtime package when the venv has no editable install.
REPO_ROOT = Path(__file__).resolve().parents[1]
if __package__ in (None, "") and str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(REPO_ROOT / "src") not in sys.path:
    try:
        import computer_use_mcp  # noqa: F401
    except ImportError:
        sys.path.insert(0, str(REPO_ROOT / "src"))

from benchmarks.appwin import (
    LaunchedApp,
    calc_button_grid,
    calc_display_value,
    focus_window,
    launch_calculator,
    launch_edge,
    launch_notepad,
    move_window,
    read_edit_text,
    window_rect,
    window_text,
)
from benchmarks.fakeworld import FakeWorldBackend
from computer_use_mcp import server
from computer_use_mcp.backend import LocalComputerBackend
from computer_use_mcp.models import GroundedAction
from computer_use_mcp.state import SessionRegistry

DISCLAIMER = "Harness validation output — NOT benchmark scores."
CATEGORIES = (
    "long_horizon_state_tracking",
    "hidden_state",
    "cross_source_reasoning",
    "visual_spatial_precision",
    "verification",
    "safety_compliance",
)
VERIFICATION_KINDS = (
    "window_text_contains",
    "window_title_contains",
    "file_contains",
    "calc_display_equals",
    "runtime_blocked",
)
ACTION_STEP_TYPES = (
    "type_text",
    "click_window_center",
    "click_button",
    "click_point",
    "key",
    "wait",
    "done",
)
HOOK_STEP_TYPES = ("hook_move_window", "hook_focus_window")

FAKE_WINDOW_DEFAULTS = {
    "notepad": {"process_name": "notepad.exe", "window_class": "Notepad"},
    "calculator": {"process_name": "win32calc.exe", "window_class": "CalcFrame"},
}


# --- task model ---------------------------------------------------------------------------------


@dataclass
class TaskSpec:
    """One benchmark task (validated; see tasks/*.yaml and README.md)."""

    id: str
    category: str
    goal: str
    setup: dict[str, Any]
    provider_steps: list[dict[str, Any]]
    verification: dict[str, Any]
    max_actions: int
    safety_notes: str
    fake_runnable: bool = True
    allowed_processes: list[str] = field(default_factory=list)


def load_tasks(tasks_dir: Path) -> list[TaskSpec]:
    """Load and validate all task files (JSON-compatible YAML subset via stdlib json)."""
    tasks: list[TaskSpec] = []
    paths = sorted(tasks_dir.glob("*.yaml")) + sorted(tasks_dir.glob("*.yml"))
    if not paths:
        raise FileNotFoundError(f"no task files found in {tasks_dir}")
    for path in paths:
        tasks.append(_validate_task(json.loads(path.read_text(encoding="utf-8")), path))
    return tasks


def _validate_task(data: Any, path: Path) -> TaskSpec:
    def need(key: str, container: Any = None) -> Any:
        source = data if container is None else container
        if key not in source:
            raise ValueError(f"{path.name}: missing required key {key!r}")
        return source[key]

    category = need("category")
    if category not in CATEGORIES:
        raise ValueError(f"{path.name}: unknown category {category!r} (known: {CATEGORIES})")
    verification = need("verification")
    kinds = (
        [verification["kind"]]
        if "kind" in verification
        else [item.get("kind") for item in verification.get("all_of", [])]
    )
    if not kinds or any(kind not in VERIFICATION_KINDS for kind in kinds):
        raise ValueError(f"{path.name}: unknown verification kinds {kinds!r}")
    provider = need("provider")
    steps = provider.get("steps") if isinstance(provider, dict) else None
    if not steps or not isinstance(steps, list):
        raise ValueError(f"{path.name}: provider.steps must be a non-empty list")
    for index, step in enumerate(steps):
        step_type = step.get("type")
        if step_type not in ACTION_STEP_TYPES + HOOK_STEP_TYPES:
            raise ValueError(f"{path.name}: step {index} has unknown type {step_type!r}")
    return TaskSpec(
        id=need("id"),
        category=category,
        goal=need("goal"),
        setup=need("setup"),
        provider_steps=steps,
        verification=verification,
        max_actions=int(need("max_actions")),
        safety_notes=str(need("safety_notes")),
        fake_runnable=bool(data.get("fake_runnable", True)),
        allowed_processes=list(data.get("allowed_processes", [])),
    )


# --- scripted provider --------------------------------------------------------------------------


class StepResolver:
    """Turns provider steps into grounded actions using current world/observation state.

    ``click_window_center`` resolves from the OBSERVATION the decision is grounded on
    when ``from_observation`` is set (pre-fault coordinates, mirroring real grounding);
    by default it resolves the window's CURRENT bounds (a fresh re-ground).
    """

    def __init__(self, mode: str, world: Any) -> None:
        self.mode = mode
        self.world = world

    def resolve(self, step: dict[str, Any], observation: Any = None) -> GroundedAction:
        step_type = step["type"]
        if step_type == "type_text":
            text = str(step["text"])
            page2_url = getattr(self.world, "page2_url", None)
            if page2_url and "{{page2_url}}" in text:
                text = text.replace("{{page2_url}}", page2_url)
            return GroundedAction(
                action="type", text=text, confidence=1.0,
                reason="benchmark scripted typing",
            )
        if step_type == "click_window_center":
            x, y = self._window_center(step, observation)
            return self._click(x, y, step)
        if step_type == "click_button":
            x, y = self.world.button_coordinates(step["label"])
            return self._click(x, y, step)
        if step_type == "click_point":
            return self._click(int(step["x"]), int(step["y"]), step)
        if step_type == "key":
            return GroundedAction(
                action="keypress", keys=list(step["keys"]), confidence=1.0,
                reason="benchmark scripted keypress",
            )
        if step_type == "wait":
            return GroundedAction(
                action="wait", delta=int(step.get("delta", 1)), confidence=1.0,
                reason="benchmark scripted wait",
            )
        raise ValueError(f"cannot resolve action step type {step_type!r}")

    def _window_center(self, step: dict[str, Any], observation: Any) -> tuple[int, int]:
        if step.get("from_observation"):
            info = (
                observation.get("active_window_info")
                if isinstance(observation, dict)
                else observation.active_window_info
            )
            if info is None:
                raise RuntimeError("observation carries no window to ground on")
            bounds = info.get("bounds") if isinstance(info, dict) else info.bounds
            if not bounds:
                raise RuntimeError("observation window has no bounds")
        else:
            bounds = self.world.window_bounds(step["window"])
        return (bounds[0] + bounds[2] // 2, bounds[1] + bounds[3] // 2)

    @staticmethod
    def _click(x: int, y: int, step: dict[str, Any]) -> GroundedAction:
        return GroundedAction(
            action="click",
            point={"x": x, "y": y},
            confidence=1.0,
            expected_effect=step.get("expected_effect"),
            reason=str(step.get("reason", "benchmark scripted click")),
        )


class DirectCallDriver:
    """Host-side step driver (RETARGETED, run_goal removal): resolves each scripted
    provider step to a grounded action and issues it as ONE ``computer_execute`` call,
    consuming the verification outcome per action. Hook steps run at their position —
    the same mid-flight fault window the loop's decide-time hooks used. This is the
    seam where a real vision provider would ground measured benchmark actions.
    """

    def __init__(self, task: TaskSpec, resolver: StepResolver) -> None:
        self.task = task
        self.resolver = resolver
        self.hooks: dict[int, dict[str, Any]] = {}
        self.action_steps: list[dict[str, Any]] = []
        for step in task.provider_steps:
            if step["type"] in HOOK_STEP_TYPES:
                self.hooks[len(self.action_steps)] = step
            else:
                self.action_steps.append(step)

    async def run(self, session_id: str) -> dict[str, Any]:
        """Execute every action step as one direct computer_execute call.

        Returns a loop-shaped summary for the metrics collector: ``results`` carries
        one entry per action (ok / action / verification as the tool returned them),
        ``termination_reason`` is derived from the LAST action's outcome, and
        ``step_count`` counts executed host actions. The loop's auto-recovery is gone;
        a failed action ENDS the task (the harness records the miss honestly — the
        host-driver contract the five-tool surface exposes to every consumer).
        """
        results: list[dict[str, Any]] = []
        executed = 0
        termination = "completed"
        last_payload: dict[str, Any] = {}
        for index, step in enumerate(self.action_steps):
            hook = self.hooks.get(index)
            if hook is not None:
                self.resolver.world.run_hook(hook)
            if step["type"] == "done":
                break  # scripted completion marker: nothing further to execute
            observation = None
            if any(
                s.get("from_observation")
                for s in [step]
            ):
                observation = self._observe(session_id)
            action = self.resolver.resolve(step, observation)
            payload = await self._execute(session_id, action, step)
            last_payload = payload
            ok = bool(payload.get("ok"))
            results.append(
                {
                    "ok": ok,
                    "action": {
                        "action": getattr(action.action, "value", action.action),
                        "point": getattr(action, "point", None),
                        "grounding": getattr(action, "grounding", None) or {},
                    },
                    "verification": payload.get("verification") or {},
                    "message": payload.get("message", ""),
                }
            )
            executed += 1
            if not ok:
                # A refused/failed action ends the host script: map the typed outcome
                # to the loop-era termination vocabulary the collector understands.
                termination = self._termination_for(payload)
                break
        return {
            "ok": termination == "completed",
            "results": results,
            "step_count": executed,
            "termination_reason": termination,
            "last_payload": last_payload,
            "metrics": None,  # filled by the collector from the real bundle
        }

    def _observe(self, session_id: str) -> Any:
        import json as _json

        from computer_use_mcp import server as srv

        response = srv.computer_observe(session_id)
        if isinstance(response, list):  # [TextContent, ImageContent]
            return _json.loads(response[0].text).get("observation")
        if isinstance(response, dict):
            return response.get("observation", response)
        return response

    async def _execute(self, session_id: str, action: GroundedAction, step: dict[str, Any]) -> dict[str, Any]:
        import json as _json

        from computer_use_mcp import server as srv

        kwargs: dict[str, Any] = {}
        if action.point is not None:
            kwargs["x"] = action.point.x
            kwargs["y"] = action.point.y
        if action.text:
            kwargs["text"] = action.text
        if action.keys:
            kwargs["keys"] = list(action.keys)
        if action.action == "wait" or getattr(action.action, "value", action.action) == "wait":
            kwargs["delta"] = action.delta or 1
        if action.expected_effect:
            kwargs["expected_effect"] = action.expected_effect
        response = await srv.computer_execute(session_id, str(getattr(action.action, "value", action.action)), **kwargs)
        if isinstance(response, list):  # executed shape: [TextContent, ImageContent]
            return _json.loads(response[0].text)
        return response

    @staticmethod
    def _termination_for(payload: dict[str, Any]) -> str:
        """Map a refused direct call to the loop-era termination vocabulary."""
        error = str(payload.get("error", "") or "")
        # Safety denials carry {"ok": False, "message": <policy reason>} with no error
        # field; requires_approval is a separate approval_required shape. The
        # collector cross-checks blocked_safety against the safety_block COUNTER and
        # zero execution events, so this mapping cannot fake a "blocked as expected".
        if error == "limit_exceeded":
            return "limit_exceeded"
        if error == "digest_surprise":
            return "digest_surprise"
        if "requires_approval" in payload:
            return "approval_required"
        verification = payload.get("verification") or {}
        if verification.get("outcome") == "failed":
            return "failed_verification"
        if error in {"action_error"}:
            return "failed_execution"
        if "reasons" in payload:
            return "rejected"
        # Plain {"ok": False, "message": ...} with no verification: the safety gate
        # (blocked_safety) is the remaining direct-path producer of this shape.
        return "blocked_safety"


# --- worlds -------------------------------------------------------------------------------------


def _verification_checks(task: TaskSpec) -> list[dict[str, Any]]:
    return (
        [task.verification]
        if "kind" in task.verification
        else list(task.verification.get("all_of", []))
    )


def _combine_predicate(evaluated: list[dict[str, Any]]) -> dict[str, Any]:
    outcomes = {item["outcome"] for item in evaluated}
    combined = "verified" if outcomes == {"verified"} else (
        "failed" if "failed" in outcomes else "not_determined"
    )
    return {
        "kind": "all_of" if len(evaluated) > 1 else evaluated[0]["kind"],
        "outcome": combined,
        "evidence": {"checks": evaluated},
    }


def _predicate_result(kind: str, ok: bool | None, evidence: dict[str, Any]) -> dict[str, Any]:
    return {"kind": kind, "outcome": {True: "verified", False: "failed", None: "not_determined"}[ok],
            "evidence": evidence}


def _file_check(check: dict[str, Any], task: TaskSpec, scratch_dir: Path) -> dict[str, Any]:
    file_path = scratch_dir / str(task.setup.get("file_name", "task.txt"))
    content = file_path.read_text(encoding="utf-8") if file_path.exists() else None
    value = check.get("value")
    return _predicate_result(
        "file_contains",
        bool(content) and value in content,
        {"file": str(file_path), "content": (content or "")[:200]},
    )


class FakeWorld:
    """Fake-mode world: a FakeWorldBackend plus resolution/final-predicate logic."""

    def __init__(self) -> None:
        self.backend = FakeWorldBackend()

    def setup(self, task: TaskSpec, scratch_dir: Path) -> None:
        app = task.setup.get("app")
        if app == "notepad":
            file_path = scratch_dir / str(task.setup.get("file_name", "task.txt"))
            file_path.write_text("", encoding="utf-8")
            self.backend.add_window(
                "target",
                title=f"{file_path.name} - Notepad",
                bounds=tuple(task.setup.get("bounds", (60, 120, 700, 450))),
                kind="notepad",
                file_path=file_path,
                **FAKE_WINDOW_DEFAULTS["notepad"],
            )
        elif app == "notepad_two":
            target_file = scratch_dir / str(task.setup.get("file_name", "target_a.txt"))
            decoy_file = scratch_dir / str(task.setup.get("decoy_file", "decoy_b.txt"))
            target_file.write_text("", encoding="utf-8")
            decoy_file.write_text("", encoding="utf-8")
            self.backend.add_window(
                "decoy",
                title=f"{decoy_file.name} - Notepad",
                bounds=tuple(task.setup.get("decoy_bounds", (300, 200, 700, 450))),
                kind="notepad",
                file_path=decoy_file,
                **FAKE_WINDOW_DEFAULTS["notepad"],
            )
            self.backend.add_window(
                "target",
                title=f"{target_file.name} - Notepad",
                bounds=tuple(task.setup.get("bounds", (60, 120, 700, 450))),
                kind="notepad",
                file_path=target_file,
                **FAKE_WINDOW_DEFAULTS["notepad"],
            )
        elif app == "calculator":
            self.backend.add_window(
                "calc",
                title="Calculator",
                bounds=tuple(task.setup.get("bounds", (96, 96, 282, 403))),
                kind="calculator",
                **FAKE_WINDOW_DEFAULTS["calculator"],
            )
        else:
            raise ValueError(f"fake mode cannot set up app {app!r} for task {task.id!r}")

    def run_hook(self, step: dict[str, Any]) -> None:
        if step["type"] == "hook_move_window":
            self.backend.move_window(step["window"], tuple(step["to"]))
        elif step["type"] == "hook_focus_window":
            self.backend.focus(step["window"])

    def window_bounds(self, key: str) -> tuple[int, int, int, int]:
        return self.backend.window_bounds(key)

    def window_center(self, key: str) -> tuple[int, int]:
        left, top, width, height = self.backend.window_bounds(key)
        return (left + width // 2, top + height // 2)

    def button_coordinates(self, label: str) -> tuple[int, int]:
        return self.backend.button_grid("calc")[label]

    def final_predicate(self, task: TaskSpec, scratch_dir: Path) -> dict[str, Any]:
        evaluated = [self._evaluate(check, task, scratch_dir) for check in _verification_checks(task)]
        return _combine_predicate(evaluated)

    def _evaluate(self, check: dict[str, Any], task: TaskSpec, scratch_dir: Path) -> dict[str, Any]:
        kind = check["kind"]
        value = check.get("value")
        if kind == "window_text_contains":
            actual = self.backend.window_text("target")
            return _predicate_result(kind, value in actual, {"window_text": actual[:200]})
        if kind == "window_title_contains":
            actual = self.backend.windows["target"].title
            return _predicate_result(kind, value.casefold() in actual.casefold(), {"title": actual})
        if kind == "file_contains":
            return _file_check(check, task, scratch_dir)
        if kind == "calc_display_equals":
            actual = self.backend.calculator.display
            return _predicate_result(kind, actual == str(value), {"display": actual})
        return _predicate_result(kind, None, {"note": "runtime_blocked is derived from the response"})


class RealWorld:
    """Env-mode world: real applications on this box (lifecycle owned by the runner)."""

    def __init__(self) -> None:
        self.apps: dict[str, LaunchedApp] = {}
        self.calc_grid: dict[str, tuple[int, int]] = {}
        self.page2_url: str | None = None

    def setup(self, task: TaskSpec, scratch_dir: Path) -> None:
        app = task.setup.get("app")
        if app == "notepad":
            file_path = scratch_dir / str(task.setup.get("file_name", "task.txt"))
            file_path.write_text("", encoding="utf-8")
            self.apps["target"] = launch_notepad(str(file_path))
        elif app == "notepad_two":
            target_file = scratch_dir / str(task.setup.get("file_name", "target_a.txt"))
            decoy_file = scratch_dir / str(task.setup.get("decoy_file", "decoy_b.txt"))
            target_file.write_text("", encoding="utf-8")
            decoy_file.write_text("", encoding="utf-8")
            self.apps["decoy"] = launch_notepad(str(decoy_file))
            move_window(self.apps["decoy"].hwnd, *task.setup.get("decoy_bounds", (300, 200, 700, 450)))
            self.apps["target"] = launch_notepad(str(target_file))
            move_window(self.apps["target"].hwnd, *task.setup.get("bounds", (60, 120, 700, 450)))
            focus_window(self.apps["target"].hwnd)
        elif app == "calculator":
            self.apps["calc"] = launch_calculator()
            self.calc_grid = calc_button_grid(self.apps["calc"].hwnd)
        elif app == "browser":
            title_marker = str(task.setup.get("title_marker", "Benchmark Page"))
            title_marker_2 = task.setup.get("title_marker_2")
            page_path = scratch_dir / "page1.html"
            page_path.write_text(
                "<!DOCTYPE html><html><head><title>" + title_marker + "</title></head>"
                "<body><h1>benchmark page 1</h1></body></html>",
                encoding="utf-8",
            )
            if title_marker_2:
                page2_path = scratch_dir / "page2.html"
                page2_path.write_text(
                    "<!DOCTYPE html><html><head><title>" + str(title_marker_2) + "</title></head>"
                    "<body><h1>benchmark page 2</h1></body></html>",
                    encoding="utf-8",
                )
                self.page2_url = "file:///" + str(page2_path).replace("\\", "/")
            self.apps["browser"] = launch_edge(
                "file:///" + str(page_path).replace("\\", "/"), title_marker
            )
        else:
            raise ValueError(f"env mode cannot set up app {app!r} for task {task.id!r}")

    def teardown(self) -> None:
        for app in self.apps.values():
            app.close()
        self.apps.clear()

    def run_hook(self, step: dict[str, Any]) -> None:
        if step["type"] == "hook_move_window":
            move_window(self.apps[step["window"]].hwnd, *step["to"])
        elif step["type"] == "hook_focus_window" and not focus_window(self.apps[step["window"]].hwnd):
            raise RuntimeError(f"could not focus {step['window']!r} for fault injection")

    def window_bounds(self, key: str) -> tuple[int, int, int, int]:
        return window_rect(self.apps[key].hwnd)

    def window_center(self, key: str) -> tuple[int, int]:
        left, top, width, height = window_rect(self.apps[key].hwnd)
        return (left + width // 2, top + height // 2)

    def button_coordinates(self, label: str) -> tuple[int, int]:
        return self.calc_grid[label]

    def final_predicate(self, task: TaskSpec, scratch_dir: Path) -> dict[str, Any]:
        evaluated = [self._evaluate(check, task, scratch_dir) for check in _verification_checks(task)]
        return _combine_predicate(evaluated)

    def _evaluate(self, check: dict[str, Any], task: TaskSpec, scratch_dir: Path) -> dict[str, Any]:
        kind = check["kind"]
        value = check.get("value")
        if kind == "window_text_contains":
            actual = read_edit_text(self.apps["target"].hwnd)
            return _predicate_result(kind, value in actual, {"window_text": actual[:200]})
        if kind == "window_title_contains":
            actual = window_text(self.apps["target"].hwnd)
            return _predicate_result(kind, value.casefold() in actual.casefold(), {"title": actual})
        if kind == "file_contains":
            return _file_check(check, task, scratch_dir)
        if kind == "calc_display_equals":
            actual = calc_display_value(self.apps["calc"].hwnd)
            return _predicate_result(kind, actual == str(value), {"display": actual})
        return _predicate_result(kind, None, {"note": "runtime_blocked is derived from the response"})


# --- the runner ----------------------------------------------------------------------------------


class BenchmarkRunner:
    def __init__(self, mode: str, tasks_dir: Path, results_dir: Path, run_id: str) -> None:
        self.mode = mode
        self.tasks_dir = tasks_dir
        self.results_dir = results_dir
        self.run_id = run_id
        self.tasks = load_tasks(tasks_dir)
        self.audit_root = Path(tempfile.gettempdir()) / "cumcp_bench" / run_id

    def run(self) -> dict[str, Any]:
        started_at = datetime.now(UTC).isoformat()
        self.audit_root.mkdir(parents=True, exist_ok=True)
        if self.mode == "env":
            from .appwin import set_dpi_awareness

            set_dpi_awareness()  # physical-pixel rect reads for the env-mode lifecycle
        original_env = os.environ.get("COMPUTER_USE_MCP_LOG_DIR")
        os.environ["COMPUTER_USE_MCP_LOG_DIR"] = str(self.audit_root)
        try:
            results = [self._run_task(task) for task in self.tasks]
        finally:
            if original_env is None:
                os.environ.pop("COMPUTER_USE_MCP_LOG_DIR", None)
            else:
                os.environ["COMPUTER_USE_MCP_LOG_DIR"] = original_env
        return {
            "run_id": self.run_id,
            "mode": self.mode,
            "disclaimer": DISCLAIMER,
            "started_at": started_at,
            "finished_at": datetime.now(UTC).isoformat(),
            "environment": {"platform": platform.platform(), "python": platform.python_version()},
            "tasks": results,
            "summary": self._summarize(results),
        }

    def _run_task(self, task: TaskSpec) -> dict[str, Any]:
        if self.mode == "fake" and not task.fake_runnable:
            return {
                "task": task.id,
                "category": task.category,
                "mode": self.mode,
                "status": "requires_env",
                "completed": False,
                "note": "task needs a real application; not modeled in fake mode",
            }
        scratch_dir = self.results_dir / "scratch" / f"{self.run_id}_{task.id}"
        scratch_dir.mkdir(parents=True, exist_ok=True)
        world: Any = FakeWorld() if self.mode == "fake" else RealWorld()
        driver = DirectCallDriver(task, StepResolver(self.mode, world))
        result: dict[str, Any] = {
            "task": task.id,
            "category": task.category,
            "mode": self.mode,
            "status": "failed",
            "completed": False,
        }
        saved_state = self._isolate_server()
        try:
            try:
                world.setup(task, scratch_dir)
            except Exception as exc:  # noqa: BLE001 - environment gaps are honest outcomes
                result.update(
                    status="requires_env" if self.mode == "fake" else "failed",
                    note=f"setup failed: {type(exc).__name__}: {exc}",
                )
                return result
            backend = world.backend if self.mode == "fake" else LocalComputerBackend()
            server._backend_factory = lambda: backend  # type: ignore[method-assign]
            response = server.start_session(
                dry_run=False,
                require_approval=False,
                max_steps=task.max_actions + 4,
                allowed_processes=task.allowed_processes or None,
            )
            if not response.get("session_id"):
                result["error"] = f"start_session failed: {response}"
                return result
            session_id = str(response["session_id"])
            run_response = asyncio.run(driver.run(session_id))
            bundle = server._get_bundle(session_id)
            # The real session metrics ride on the bundle (the loop response used to
            # carry them); hand them to the collector in the same shape.
            metrics_snapshot = bundle.metrics.snapshot()
            run_response["metrics"] = metrics_snapshot
            result.update(self._collect(task, run_response, bundle, session_id, world, scratch_dir))
            server.stop_session(session_id)
            return result
        except Exception as exc:  # noqa: BLE001 - harness failures are recorded, never raised
            result["error"] = f"{type(exc).__name__}: {exc}"
            return result
        finally:
            (
                server._registry,
                server._bundles,
                server._backend_factory,
                server._provider_factory,
            ) = saved_state
            if self.mode == "env":
                world.teardown()

    @staticmethod
    def _isolate_server() -> tuple[Any, Any, Any, Any]:
        """Swap in fresh session wiring for one task; the snapshot is restored by the caller.

        Fresh registry/bundles per task also prevent the bounded session registry from
        exhausting across tasks (stop_session arms the kill path but never deregisters).
        """
        snapshot = (server._registry, server._bundles, server._backend_factory, server._provider_factory)
        server._registry = SessionRegistry(max_sessions=8)
        server._bundles = {}
        return snapshot

    def _collect(
        self,
        task: TaskSpec,
        run_response: dict[str, Any],
        bundle: Any,
        session_id: str,
        world: Any,
        scratch_dir: Path,
    ) -> dict[str, Any]:
        results = run_response.get("results", [])
        verification_counts = {"verified": 0, "failed": 0, "uncertain": 0, "other": 0}
        grounding_strategies: dict[str, int] = {}
        grounding_confidences: list[float] = []
        executed_actions = 0
        for item in results:
            action = item.get("action") or {}
            verification = item.get("verification") or {}
            outcome = verification.get("outcome", "other")
            verification_counts[outcome if outcome in verification_counts else "other"] += 1
            grounding = action.get("grounding") or {}
            strategy = grounding.get("strategy")
            if strategy:
                grounding_strategies[strategy] = grounding_strategies.get(strategy, 0) + 1
                confidence = grounding.get("confidence")
                if confidence is not None:
                    grounding_confidences.append(float(confidence))
        audit_events: list[dict[str, Any]] = []
        audit_path = bundle.auditor.path_for(session_id)
        if audit_path.exists():
            audit_events = [
                json.loads(line)
                for line in audit_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        recovery_classes: dict[str, int] = {}
        for event in audit_events:
            if event.get("event_type") == "recovery":
                failure_class = (event.get("metadata") or {}).get("failure_class") or "unknown"
                recovery_classes[failure_class] = recovery_classes.get(failure_class, 0) + 1
        safety_blocks = int(bundle.metrics.snapshot()["counters"].get("safety_block", 0))
        expected_block = task.verification.get("kind") == "runtime_blocked" or any(
            check.get("kind") == "runtime_blocked" for check in task.verification.get("all_of", [])
        )
        execution_events = sum(
            1
            for event in audit_events
            if event.get("event_type") == "execution" and event.get("result") == "ok"
        )
        executed_actions = execution_events
        if expected_block:
            safety_violations = 1 if execution_events else 0
            false_blocks = 0
        else:
            safety_violations = 0
            false_blocks = safety_blocks
        termination = run_response.get("termination_reason")
        predicate = world.final_predicate(task, scratch_dir)
        if expected_block:
            blocked_as_expected = termination == "blocked_safety" and execution_events == 0
            predicate["outcome"] = "verified" if blocked_as_expected else "failed"
            predicate["evidence"]["termination_reason"] = termination
            predicate["evidence"]["execution_events"] = execution_events
        completed = termination == "completed"
        predicate_ok = predicate["outcome"] in {"verified", "not_determined"}
        status = "completed" if completed and predicate_ok else "failed"
        if expected_block and predicate["outcome"] == "verified":
            status = "safety_blocked_as_expected"
        return {
            "task": task.id,
            "category": task.category,
            "mode": self.mode,
            "status": status,
            "completed": completed,
            "termination_reason": termination,
            "actions_taken": run_response.get("step_count", 0),
            "executed_actions": executed_actions,
            "max_actions": task.max_actions,
            "verification_outcomes": verification_counts,
            "grounding": {
                "strategies": grounding_strategies,
                "avg_confidence": (
                    sum(grounding_confidences) / len(grounding_confidences)
                    if grounding_confidences
                    else None
                ),
            },
            "recovery_events": sum(recovery_classes.values()),
            "recovery_classes": recovery_classes,
            "safety_blocks": safety_blocks,
            "expected_safety_block": expected_block,
            "safety_violations": safety_violations,
            "false_safety_blocks": false_blocks,
            "final_predicate": predicate,
            "latencies_ms": (run_response.get("metrics") or {}).get("latencies", {}),
            "model_calls": (run_response.get("metrics") or {}).get("counters", {}).get("model_calls", 0),
            "audit_events": len(audit_events),
            "error": None,
        }

    @staticmethod
    def _summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
        actionable = [item for item in results if item.get("status") != "requires_env"]
        completed = sum(
            1
            for item in actionable
            if item.get("status") in {"completed", "safety_blocked_as_expected"}
        )
        actions = [item.get("actions_taken", 0) for item in actionable]
        return {
            "disclaimer": DISCLAIMER,
            "tasks_total": len(results),
            "tasks_run": len(actionable),
            "tasks_requires_env": len(results) - len(actionable),
            "completed": completed,
            "failed": len(actionable) - completed,
            "recovery_events_total": sum(item.get("recovery_events", 0) for item in actionable),
            "safety_blocks_total": sum(item.get("safety_blocks", 0) for item in actionable),
            "false_safety_blocks_total": sum(item.get("false_safety_blocks", 0) for item in actionable),
            "safety_violations_total": sum(item.get("safety_violations", 0) for item in actionable),
            "actions_per_task_avg": (sum(actions) / len(actions)) if actions else 0.0,
        }


def print_summary_table(payload: dict[str, Any]) -> None:
    print(f"\n{DISCLAIMER}")
    print(f"run {payload['run_id']} (mode={payload['mode']})")
    header = f"{'task':<38} {'category':<30} {'status':<28} {'actions':>7} {'recover':>7}"
    print(header)
    print("-" * len(header))
    for item in payload["tasks"]:
        print(
            f"{item.get('task', '?'):<38} {item.get('category', '?'):<30} "
            f"{item.get('status', '?'):<28} {item.get('actions_taken', 0):>7} "
            f"{item.get('recovery_events', 0):>7}"
        )
    summary = payload["summary"]
    print("-" * len(header))
    print(
        f"completed {summary['completed']}/{summary['tasks_run']} run "
        f"({summary['tasks_requires_env']} requires_env) | recovery {summary['recovery_events_total']} "
        f"| safety blocks {summary['safety_blocks_total']} "
        f"(false {summary['false_safety_blocks_total']}, violations {summary['safety_violations_total']}) "
        f"| avg actions/task {summary['actions_per_task_avg']:.1f}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="computer-use-mcp benchmark harness (no scores)")
    parser.add_argument("--mode", choices=("fake", "env"), default="fake")
    parser.add_argument("--tasks", type=Path, default=Path(__file__).resolve().parent / "tasks")
    parser.add_argument("--results", type=Path, default=Path(__file__).resolve().parent / "results")
    parser.add_argument("--run-id", default=datetime.now(UTC).strftime("%Y%m%d-%H%M%S"))
    args = parser.parse_args(argv)
    runner = BenchmarkRunner(args.mode, args.tasks, args.results, args.run_id)
    payload = runner.run()
    args.results.mkdir(parents=True, exist_ok=True)
    out_path = args.results / f"{args.run_id}.json"
    out_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print_summary_table(payload)
    print(f"results written to {out_path}")
    return 0 if payload["summary"]["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
