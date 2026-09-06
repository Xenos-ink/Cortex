"""Runtime-side E2E helpers: scripted provider + real-window verification strategies.

Two pieces (both E7-owned, tests/e2e only):

``E2EScriptedProvider``
    Implements the pinned E4 provider surface (``decide_full``/``decide``/``judge_change``)
    with a script of entries. An entry is either an :class:`AgentDecision` (e.g.
    :func:`done`) or a dict from :func:`step` carrying a ``GroundedAction`` plus a
    provider-level ``verification_hint``; a dict entry's ``"decision"`` may also be a
    zero-arg callable receiving the observation and returning a ``step()`` dict — this is
    how E2E re-decides from fresh reality (e.g. clicking the center of a window's CURRENT
    bounds). ``hooks`` are index-aligned callables run BEFORE a decision is returned
    (fault injection between propose and validate). ``decide_calls``/``fault_log`` feed
    the evidence transcripts. NO network, NO vision model: E2E is deterministic by design
    (the runtime, not the model, is what is under test).

    Hint semantics (must match ``agent._build_intent``): ``verification_hint="expected_text"``
    makes the intent verify the TYPED TEXT (so type actions must NOT set expected_effect);
    ``verification_hint="window_state"`` makes the intent verify the window title contains
    ``expected_effect`` (so the effect string must BE the title needle).

``WindowTextPredicateStrategy`` / ``CalcDisplayPredicateStrategy``
    Application-specific deterministic verification strategies (Goal.md section 7,
    strategies 5 and 7) plugged into the runtime's documented ``VerificationStrategy``
    protocol and injected into the session's ``VerificationEngine`` via the server test
    seam (``_get_bundle(session_id).agent.verifier``). They read REAL Win32 window text
    from the post-action observation's foreground window and decide verified/failed/
    uncertain from that evidence — fabricating nothing. This is the acceptance-A
    mechanism: semantic verification of real window text, not pixel diff alone.
"""

from __future__ import annotations

import re
from types import SimpleNamespace
from typing import Any

import helpers_win32 as w32

from computer_use_mcp.models import AgentDecision, GroundedAction, Observation, VerificationResult
from computer_use_mcp.verification import VerificationIntent, VerificationKind


def step(
    action: GroundedAction,
    verification_hint: str | None = None,
    summary: str = "E2E scripted decision",
) -> dict[str, Any]:
    """One scripted decision entry: an action plus its provider-level verification hint."""
    return {
        "action": action,
        "verification_hint": verification_hint,
        "summary": summary,
        "expected_effect": action.expected_effect,
    }


def done(summary: str = "E2E scripted completion") -> AgentDecision:
    return AgentDecision(status="done", summary=summary)


def click(x: int, y: int, expected_effect: str | None = None, reason: str = "E2E click") -> GroundedAction:
    return GroundedAction(
        action="click",
        point={"x": x, "y": y},
        confidence=1.0,
        expected_effect=expected_effect,
        reason=reason,
    )


def type_text(text: str, expected_effect: str | None = None) -> GroundedAction:
    return GroundedAction(
        action="type",
        text=text,
        confidence=1.0,
        expected_effect=expected_effect,
        reason="E2E scripted typing",
    )


def _envelope(entry: dict[str, Any] | AgentDecision) -> SimpleNamespace:
    if isinstance(entry, AgentDecision):
        return SimpleNamespace(
            decision=entry,
            expected_effect=entry.expected_change,
            verification_hint=None,
            suspicious_content=None,
            redactions_applied=[],
        )
    action = entry["action"]
    decision = AgentDecision(status="action", action=action, summary=str(entry.get("summary", "")))
    return SimpleNamespace(
        decision=decision,
        expected_effect=entry.get("expected_effect"),
        verification_hint=entry.get("verification_hint"),
        suspicious_content=None,
        redactions_applied=[],
    )


class E2EScriptedProvider:
    """Deterministic scripted provider (no external API) implementing the E4 surface."""

    def __init__(
        self,
        script: list[Any],
        *,
        hooks: list[Any] | None = None,
        repeat_last: bool = False,
    ) -> None:
        self.script = list(script)
        self.hooks = list(hooks or [])
        self.repeat_last = repeat_last
        self.decide_calls = 0
        self.judge_calls = 0
        self._script_index = 0
        self.fault_log: list[dict[str, Any]] = []

    def _resolve_entry(self, entry: Any, observation: Any) -> dict[str, Any] | AgentDecision:
        if callable(entry) and not isinstance(entry, AgentDecision):
            entry = entry(observation)  # dynamic decision, computed from fresh reality
        if isinstance(entry, dict) and callable(entry.get("decision")):
            entry = entry["decision"](observation)
        if isinstance(entry, dict) and callable(entry.get("action")):
            raise TypeError("step() entries must carry a GroundedAction, not a callable action.")
        return entry

    def _next(self, goal: str, observation: Any, history: list[str]) -> Any:
        call = self.decide_calls
        self.decide_calls += 1
        if call < len(self.hooks) and self.hooks[call] is not None:
            self.hooks[call](goal, observation, history)
        if self._script_index >= len(self.script):
            if not self.repeat_last:
                raise RuntimeError(
                    f"E2EScriptedProvider script exhausted after {self.decide_calls} decide calls "
                    f"(goal={goal[:80]!r})."
                )
            self._script_index = len(self.script) - 1
        entry = self._resolve_entry(self.script[self._script_index], observation)
        self._script_index += 1
        return _envelope(entry)

    async def decide_full(self, goal: str, observation: Any, history: list[str]) -> Any:
        return self._next(goal, observation, history)

    async def decide(self, goal: str, observation: Any, history: list[str]) -> Any:
        envelope = self._next(goal, observation, history)
        return envelope.decision

    def judge_change(self, before_b64: str, after_b64: str, expected_effect: str) -> dict[str, Any]:
        self.judge_calls += 1
        return {"outcome": "uncertain", "confidence": 0.0, "reason": "E2E judge is always uncertain"}


class WindowTextPredicateStrategy:
    """Verified/failed from the REAL child-control text of the foreground window.

    ``can_verify`` claims the ``expected_text`` kind; without OCR the built-in
    TextPredicateStrategy degrades to uncertain, while this strategy reads the actual
    window content via WM_GETTEXT — deterministic, evidence-based, real.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    @property
    def name(self) -> str:
        return "window_text"

    def can_verify(self, intent: VerificationIntent) -> bool:
        return intent.kind == VerificationKind.EXPECTED_TEXT

    def verify(
        self, intent: VerificationIntent, before: Observation, after: Observation
    ) -> VerificationResult:
        expected = intent.expected_text
        info = after.active_window_info
        record: dict[str, Any] = {"expected": expected, "hwnd": info.hwnd if info else None}
        self.calls.append(record)
        if not expected:
            return self._result("uncertain", "No expected text stated.", record, 0.0)
        if info is None or info.hwnd is None:
            return self._result(
                "uncertain", "Post-action observation carries no window identity.", record, 0.0
            )
        try:
            text = w32.read_edit_text(info.hwnd)
        except Exception as exc:  # noqa: BLE001 - degrade, never fake success
            record["error"] = f"{type(exc).__name__}: {exc}"
            return self._result("uncertain", "Window text could not be read.", record, 0.0)
        record["window_text"] = text[:500]
        matched = expected.casefold() in text.casefold()
        record["matched"] = matched
        if matched:
            return self._result("verified", "Expected text found in the real window text.", record, 0.9)
        return self._result("failed", "Expected text is absent from the real window text.", record, 0.85)

    def _result(
        self, outcome: str, note: str, record: dict[str, Any], confidence: float
    ) -> VerificationResult:
        evidence = [f"{key}={record.get(key)!r}" for key in ("expected", "hwnd", "matched") if key in record]
        if "window_text" in record:
            evidence.append(f"window_text[:120]={record['window_text'][:120]!r}")
        if "error" in record:
            evidence.append(record["error"])
        return VerificationResult(
            outcome=outcome,  # type: ignore[arg-type]
            changed=outcome == "verified",
            note=note,
            confidence=confidence,
            evidence=evidence,
            verification_method=self.name,
        )


_CALC_DISPLAY_SPEC = re.compile(r"^calc_display_equals:(.+)$")


class CalcDisplayPredicateStrategy:
    """Deterministic predicate over the REAL Calculator display Static control.

    The intent's ``expected_effect`` must look like ``calc_display_equals:<value>``. The
    display is read from the bound Calculator hwnd (identity cross-checked against the
    post-action foreground window); any drift degrades to uncertain — never to success.

    The strategy also claims ``visual_change`` intents whose effect carries the
    ``calc_display_equals:`` marker: a single digit change on a 1920x1080 screenshot
    produces a mean pixel difference (~0.2) far below the legacy diff threshold (1.0),
    so pixel diff alone CANNOT verify Calculator input — measured live in the E2E run.
    Reading the real display control is the deterministic evidence here.
    """

    def __init__(self, calc_hwnd: int | None) -> None:
        self.calc_hwnd = calc_hwnd
        self.calls: list[dict[str, Any]] = []

    @property
    def name(self) -> str:
        return "calc_display"

    def can_verify(self, intent: VerificationIntent) -> bool:
        claimed = intent.kind in {VerificationKind.PREDICATE, VerificationKind.VISUAL_CHANGE}
        return claimed and bool(_CALC_DISPLAY_SPEC.match(intent.expected_effect or ""))

    def verify(
        self, intent: VerificationIntent, before: Observation, after: Observation
    ) -> VerificationResult:
        match = _CALC_DISPLAY_SPEC.match(intent.expected_effect or "")
        expected = match.group(1) if match else None
        info = after.active_window_info
        record: dict[str, Any] = {"expected": expected, "calc_hwnd": self.calc_hwnd}
        self.calls.append(record)
        if self.calc_hwnd is None or not w32.is_visible(self.calc_hwnd):
            return self._result("uncertain", "Bound Calculator window is gone.", record, 0.0)
        if info is not None and info.hwnd is not None and info.hwnd != self.calc_hwnd:
            record["foreground_hwnd"] = info.hwnd
            return self._result(
                "uncertain", "Foreground window is not the bound Calculator.", record, 0.0
            )
        try:
            actual = w32.calc_display_value(self.calc_hwnd)
        except Exception as exc:  # noqa: BLE001
            record["error"] = f"{type(exc).__name__}: {exc}"
            return self._result("uncertain", "Calculator display could not be read.", record, 0.0)
        record["display"] = actual
        matched = actual == expected
        record["matched"] = matched
        if matched:
            return self._result("verified", "Calculator display matches the expectation.", record, 0.95)
        return self._result("failed", "Calculator display does not match the expectation.", record, 0.9)

    def _result(
        self, outcome: str, note: str, record: dict[str, Any], confidence: float
    ) -> VerificationResult:
        evidence = [
            f"{key}={record.get(key)!r}"
            for key in ("expected", "display", "matched", "foreground_hwnd", "calc_hwnd")
            if key in record
        ]
        if "error" in record:
            evidence.append(record["error"])
        return VerificationResult(
            outcome=outcome,  # type: ignore[arg-type]
            changed=outcome == "verified",
            note=note,
            confidence=confidence,
            evidence=evidence,
            verification_method=self.name,
        )


def observation_center(observation: Any) -> tuple[int, int]:
    """Center of the active window bounds from an Observation (model or dumped dict)."""
    info = (
        observation.get("active_window_info")
        if isinstance(observation, dict)
        else observation.active_window_info
    )
    bounds = (
        info.get("bounds")
        if isinstance(info, dict)
        else getattr(info, "bounds", None)
    )
    if not bounds:
        raise AssertionError(f"observation carries no window bounds: {info}")
    return (bounds[0] + bounds[2] // 2, bounds[1] + bounds[3] // 2)
