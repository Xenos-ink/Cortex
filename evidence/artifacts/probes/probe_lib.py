"""Shared harness for E9 red-team probes (evidence/artifacts only; never a repo test).

Imports the package from the repo's ``src`` tree, resets server module state per session,
and provides the ScriptedProvider/ScriptedBackend patterns copied from
tests/test_controller_integration.py (read-only reference — no repo file is modified).
"""

from __future__ import annotations

import base64
import io
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[3]
SRC = REPO / "src"
for p in (str(SRC),):
    if p not in sys.path:
        sys.path.insert(0, p)

from computer_use_mcp import server  # noqa: E402
from computer_use_mcp.models import (  # noqa: E402
    AgentDecision,
    GroundedAction,
    TextRegion,
)
from computer_use_mcp.backend import FakeComputerBackend  # noqa: E402
from computer_use_mcp.state import SessionRegistry  # noqa: E402
from PIL import Image  # noqa: E402


def _png(color: str = "white", size: tuple[int, int] = (64, 48)) -> str:
    image = Image.new("RGB", size, color)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


class Envelope:
    """Duck-typed ProviderDecision envelope."""

    def __init__(self, decision: Any, suspicious_content: Any = None) -> None:
        self.decision = decision
        self.expected_effect = getattr(decision, "expected_effect", None)
        self.verification_hint = getattr(decision, "verification_hint", None)
        self.suspicious_content = suspicious_content
        self.redactions_applied: list[str] = []


class ScriptedProvider:
    """Script of decisions; optional errors/hooks per decide-call index."""

    def __init__(
        self,
        script: list[Any] | None = None,
        *,
        errors: list[Exception | None] | None = None,
        always_error: Exception | None = None,
        hooks: list[Any] | None = None,
        repeat_last: bool = True,
        judge: Any = None,
    ) -> None:
        self.script = list(script or [])
        self.errors = list(errors or [])
        self.always_error = always_error
        self.hooks = list(hooks or [])
        self.repeat_last = repeat_last
        self.judge_result = judge
        self.decide_calls = 0
        self.judge_calls = 0
        self.goals_seen: list[str] = []
        self._script_index = 0

    async def decide_full(self, goal: str, observation: Any, history: list[str]) -> Any:
        call = self.decide_calls
        self.decide_calls += 1
        self.goals_seen.append(goal)
        if call < len(self.hooks) and self.hooks[call] is not None:
            self.hooks[call]()
        if self.always_error is not None:
            raise self.always_error
        if call < len(self.errors) and self.errors[call] is not None:
            raise self.errors[call]
        if not self.script:
            raise RuntimeError("ScriptedProvider script is empty.")
        if self._script_index >= len(self.script) and not self.repeat_last:
            raise RuntimeError("ScriptedProvider script exhausted.")
        decision = self.script[min(self._script_index, len(self.script) - 1)]
        self._script_index += 1
        if isinstance(decision, AgentDecision):
            return Envelope(decision)
        if isinstance(decision, tuple) and len(decision) == 2:
            return Envelope(decision[0], suspicious_content=decision[1])
        return decision

    async def decide(self, goal: str, observation: Any, history: list[str]) -> Any:
        result = await self.decide_full(goal, observation, history)
        return result.decision

    def judge_change(
        self, before_b64: str, after_b64: str, expected_effect: str, goal: str | None = None
    ) -> dict[str, Any]:
        self.judge_calls += 1
        if self.judge_result is not None:
            return dict(self.judge_result)
        return {"outcome": "uncertain", "confidence": 0.0, "reason": "fake judge never verifies"}


class ScriptedBackend(FakeComputerBackend):
    """Fake backend with deterministic screenshot flipping + OCR text injection."""

    def __init__(self, *, flip: bool = True, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.flip = flip
        self.executes = 0
        self.execute_hooks: list[Any] = []
        self.typed_text: str | None = None
        self.executed_actions: list[GroundedAction] = []

    def observe(self) -> Any:
        observation = super().observe()
        color = "white"
        if self.flip and self.executes % 2 == 1:
            color = "black"
        observation.image_base64 = _png(color, (self.width, self.height))
        if self.typed_text:
            observation.ocr_text = [
                TextRegion(text=self.typed_text, x=8, y=8, width=120, height=16, confidence=0.95)
            ]
        return observation

    def execute(self, action: GroundedAction, stop: Any = None) -> str:
        for hook in self.execute_hooks:
            hook(action)
        if action.action.value == "type" and action.text:
            self.typed_text = action.text
        message = super().execute(action, stop)
        self.executes += 1
        self.executed_actions.append(action)
        return message


def reset_server(tmp_root: Path | None = None) -> Path:
    """Reset server module state + audit dir. Returns the audit root."""
    audit_root = (tmp_root or Path(tempfile.mkdtemp(prefix="e9probe-"))) / "audit"
    os.environ["COMPUTER_USE_MCP_LOG_DIR"] = str(audit_root)
    server._registry = SessionRegistry(max_sessions=8)
    server._bundles = {}
    server._stopped_sessions = {}
    server._backend_factory = FakeComputerBackend
    server._provider_factory = ScriptedProvider([])
    return audit_root


def make_session(
    backend: Any = None,
    provider: Any = None,
    **start_kwargs: Any,
) -> tuple[str, Any, Any, Any]:
    backend = backend if backend is not None else ScriptedBackend()
    provider = provider if provider is not None else ScriptedProvider([])
    server._backend_factory = lambda: backend
    server._provider_factory = lambda: provider
    response = server.start_session(**start_kwargs)
    assert response.get("session_id"), response
    session_id = str(response["session_id"])
    bundle = server._get_bundle(session_id)
    return session_id, bundle, backend, provider


def run(coro: Any) -> Any:
    import asyncio

    return asyncio.run(coro)
