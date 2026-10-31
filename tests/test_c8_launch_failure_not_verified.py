"""C8 / §1.3 MAJOR-4 (ORVEX-2026-108 WP4b) — a FAILED ``ensure_app`` launch is never
verified.

``run_single`` short-circuits an ``ensure_app`` whose message is a PROBE outcome
(``NO_INSTANCE`` / ``AMBIGUOUS_INSTANCE`` / ``LAUNCHED``): the payload makes no screen
claim, so the payload IS the answer the driver acts on and the shortcut hands it a
hand-built verification result. That is right for a probe that RAN — the absence of a
matching window IS the answer — but the shortcut must look at WHICH probe came back,
because three of them are FAILURE values:

* ``not_found`` (plain ``NO_INSTANCE`` under ``allow_launch=False``) — nothing matched
  AND nothing was asked for/allowed to launch;
* ``launch_rejected=LaunchTargetError`` — the needle failed charset validation and was
  refused; nothing was launched and nothing could have been;
* ``launch_unresolved=path-lookup-missed`` / ``launch_unresolved=spawn-failed (...)`` —
  a valid needle that resolved to nothing on PATH, or whose spawn raised.

The §1.3/WP1 mapping (this tree's authority, mirroring ``models.EnsureAppOutcome``):
``not_found`` / ``launch_rejected`` / ``launch_unresolved`` are launch FAILURES —
``result: "failed"``, ``ok: False`` (dev marked them ``uncertain``; the unified
contract demotes them to failed, consistently with every other launch failure);
``ambiguous`` stays ``uncertain`` (several candidates, the tool did not establish
which window). ``AMBIGUOUS_INSTANCE``/``LAUNCHED`` probes that ran keep ``ok: true`` —
for those the probe really did run, and running IS the answer.

The audit side (E-05): the probe's verification audit event is emitted from the SAME
branch verdict, so the audit result equals the reply verdict here too (pinned by
``test_launch_failure_audit_agrees_with_the_reply``).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from computer_use_mcp.pipeline.agent import ComputerUseAgent
from computer_use_mcp.desktop.fake_backend import FakeComputerBackend
from computer_use_mcp.desktop.launch import AppWindowCandidate
from computer_use_mcp.core.limits import Limits
from computer_use_mcp.desktop.interference import parse_interference
from computer_use_mcp.core.models import GroundedAction, WindowInfo
from computer_use_mcp.security.safety import SafetyPolicy
from computer_use_mcp.core.state import StopToken, TaskState

EXCEL = WindowInfo(
    hwnd=1, pid=100, process_name="EXCEL.EXE", window_class="XLMAIN", title="Book1 - Excel"
)


def _agent(
    backend: FakeComputerBackend, *, allow_launch: bool = True
) -> ComputerUseAgent:
    launch_policy = (
        None
        if allow_launch
        else {"attach_or_launch": {"launch": "driver"}}  # never-launch: the genuinely
        # launchless shape (the agent gate passes allow_launch=False to the backend)
    )
    return ComputerUseAgent(
        backend,
        provider=None,
        safety=SafetyPolicy(),
        task=TaskState(),
        stop=StopToken(),
        limits=Limits(min_screenshot_interval_ms=1, max_actions=100).validate(),
        allowed_processes=(
            ["EXCEL.EXE", "mspaint.exe", "evil & calc", "notepad.exe"] if allow_launch else None
        ),
        interference=parse_interference(launch_policy),
    )


def _state() -> SimpleNamespace:
    return SimpleNamespace(
        dry_run=False,
        stopped=False,
        allowed_windows=[],
        min_confidence=0.0,
        max_steps=100,
        step_count=0,
        require_approval=False,
        max_retries_per_action=1,
    )


def _probe(
    target: str,
    *,
    allow_launch: bool = True,
    unresolvable: bool = False,
) -> tuple[dict[str, Any], ComputerUseAgent, FakeComputerBackend]:
    backend = FakeComputerBackend()
    backend.windows = []
    backend.app_windows = []
    backend.set_active_window(EXCEL)
    if unresolvable:
        backend.launch_unresolvable = {target}
    agent = _agent(backend, allow_launch=allow_launch)
    outcome = asyncio.run(
        agent.run_single(
            _state(), GroundedAction(action="ensure_app", target=target, confidence=1.0), approved=True
        )
    )
    assert outcome.result is not None, outcome.reasons
    return outcome.result.model_dump(), agent, backend


# --- the failure-valued probes are NOT successes ------------------------------------------------


def test_launch_rejected_is_not_verified() -> None:
    """A needle the launcher REFUSED (charset validation) — nothing was launched and
    nothing could have been."""
    payload, _agent, _backend = _probe("evil & calc")
    assert "launch_rejected=LaunchTargetError" in payload["message"], payload["message"]
    assert payload["verification"]["outcome"] == "failed", payload["verification"]
    assert payload["verification"]["verified"] is False
    assert payload["ok"] is False
    # the probe method still names itself, and the reason rides the evidence channel
    assert payload["verification"]["verification_method"] == "ensure_app_probe"
    assert "launch_rejected" in " | ".join(payload["verification"]["evidence"])


def test_launch_unresolved_path_miss_is_not_verified() -> None:
    """A valid needle that resolved to nothing on PATH."""
    payload, _agent, _backend = _probe("mspaint.exe", unresolvable=True)
    assert payload["verification"]["outcome"] == "failed", payload["verification"]
    assert payload["ok"] is False
    assert "launch_unresolved" in " | ".join(payload["verification"]["evidence"])


def test_not_found_without_a_launch_request_is_failed_not_ok() -> None:
    """The genuinely-launchless shape: ``allow_launch=False`` and nothing matched. The
    probe RAN, but its outcome is a launch FAILURE under the §1.3/WP1 mapping —
    ``result: "failed"``, never ``ok: true``."""
    payload, _agent, _backend = _probe("mspaint.exe", allow_launch=False)
    assert payload["ensure_app_outcome"] == "not_found", payload["message"]
    assert payload["message"].startswith("NO_INSTANCE"), payload["message"]
    assert payload["verification"]["outcome"] == "failed", payload["verification"]
    assert payload["verification"]["verification_method"] == "ensure_app_probe"
    assert payload["ok"] is False


# --- the probes that really ran and succeeded keep ok:true (the documented contract) ------------


def test_a_successful_launch_still_reports_ok_true() -> None:
    """The launch the fake world CAN perform: the needle resolves and the process
    starts, so the leading token is ``LAUNCHED`` (WP4b C8: the old ``NO_INSTANCE``
    token was chosen before the spawn attempt and never revised) and the probe outcome
    is a success value — ``verified``/``ok``."""
    payload, _agent, _backend = _probe("notepad.exe")
    assert payload["message"].startswith("LAUNCHED"), payload["message"]
    assert payload["ensure_app_outcome"] == "launched"
    assert "launch_unresolved=" not in payload["message"], payload["message"]
    assert "launch_rejected=" not in payload["message"], payload["message"]
    assert payload["verification"]["verification_method"] == "ensure_app_probe"
    assert payload["verification"]["outcome"] == "verified", payload["verification"]
    assert payload["ok"] is True


def test_an_ambiguous_match_is_uncertain_never_verified() -> None:
    """``ambiguous`` did NOT establish which window is the target — uncertain is never
    success (ok False), and it is NOT one of the launch failures either."""
    a = WindowInfo(hwnd=11, pid=110, process_name="EXCEL.EXE", title="Book1 - Excel")
    b = WindowInfo(hwnd=12, pid=111, process_name="EXCEL.EXE", title="Budget - Excel")
    backend = FakeComputerBackend()
    backend.set_active_window(EXCEL)
    backend.set_windows([a, b])
    backend.app_windows = [
        AppWindowCandidate(window=a, doc_token="book1", unsaved_candidate=True),
        AppWindowCandidate(window=b, doc_token="budget", unsaved_candidate=False),
    ]
    outcome = asyncio.run(
        _agent(backend).run_single(
            _state(),
            GroundedAction(action="ensure_app", target="excel|nomatch", confidence=1.0),
            approved=True,
        )
    )
    assert outcome.result is not None
    payload = outcome.result.model_dump()
    assert payload["ensure_app_outcome"] == "ambiguous"
    assert payload["verification"]["outcome"] == "uncertain", payload["verification"]
    assert payload["ok"] is False
    assert backend.launched_processes == []


# --- the classifier itself ---------------------------------------------------------------------


def test_the_probe_failure_classifier_names_only_the_two_failure_values() -> None:
    """Unit pin on the parser: the two failure suffixes are the whole vocabulary, and a
    probe with no failure suffix is not a failure."""
    from computer_use_mcp.pipeline.agent import _ensure_app_probe_failure

    assert _ensure_app_probe_failure("NO_INSTANCE target='a' launch=server") is None
    assert _ensure_app_probe_failure("AMBIGUOUS_INSTANCE candidates=[]") is None
    assert (
        _ensure_app_probe_failure("NO_INSTANCE target='a' launch=server launched=a.exe")
        is None
    )
    assert (
        _ensure_app_probe_failure("NO_INSTANCE target='a' launch_rejected=LaunchTargetError")
        == "launch_rejected"
    )
    assert (
        _ensure_app_probe_failure(
            "NO_INSTANCE target='a' launch_unresolved=path-lookup-missed (x)"
        )
        == "launch_unresolved"
    )
    assert (
        _ensure_app_probe_failure(
            "NO_INSTANCE target='a' launch_unresolved=spawn-failed (PermissionError: nope)"
        )
        == "launch_unresolved"
    )
    assert _ensure_app_probe_failure("Executed type. integrity=verified(4/4)") is None
    assert _ensure_app_probe_failure("") is None


def test_the_probe_shortcut_is_not_taken_for_a_launched_target() -> None:
    """``REATTACHED`` is not a probe outcome and never entered the shortcut before
    this fix either — the classifier is scoped to the probe prefixes."""
    from computer_use_mcp.pipeline.agent import ComputerUseAgent as Agent

    assert Agent._ensure_app_probe_outcome("REATTACHED title='Book1' hwnd=1") is False
    assert Agent._ensure_app_probe_outcome("NO_INSTANCE target='a'") is True
    # WP4b (C8 / dev F3 fix 5.1): a REAL spawn joins the screen-claim-free vocabulary.
    assert Agent._ensure_app_probe_outcome("LAUNCHED target='a' launched=a.exe") is True


# --- the typed outcome rides the reply on every one of these paths -----------------------------


def test_the_typed_outcome_matches_the_message_on_every_probe_shape() -> None:
    for target, allow_launch, unresolvable, expected in [
        ("evil & calc", True, False, "launch_rejected"),
        ("mspaint.exe", True, True, "launch_unresolved"),
        ("mspaint.exe", False, False, "not_found"),
        ("notepad.exe", True, False, "launched"),
    ]:
        payload, _agent, _backend = _probe(target, allow_launch=allow_launch, unresolvable=unresolvable)
        assert payload["ensure_app_outcome"] == expected, (target, payload["message"])
        # ok-rule consistency (§1.1): only a success-valued outcome may pair with ok.
        assert payload["ok"] is (expected == "launched"), (target, payload)
