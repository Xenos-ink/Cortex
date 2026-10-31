"""A3 / F2-1 — an R-04 ``integrity=mismatch`` read-back blocks a positive verdict.

Two producers answer two DIFFERENT questions about one ``type`` action:

1. **R-04 typed-text integrity** (``backend.py:_execute_type`` ->
   ``_format_integrity_status``) answers "does the FOCUSED CONTROL's read-back end
   with what we typed?" and reports ``verified|partial|unverified|mismatch``.  It
   deliberately refuses to repair on a disagreement (a masked/transformed buffer
   would double-apply), so ``mismatch`` is a strong, target-scoped statement: the
   authoritative reader says the text is NOT there.
2. **The agent's semantic verification** (``agent._verify`` ->
   ``UiControlTextStrategy``) answers "does the needle appear in the active WINDOW
   TITLE or in any of the first 50 UI-control name/value fields".  That is a
   SUBSTRING match on a weak surface.

Before this fix a masked-field world (read-back ``mismatch``) whose WINDOW TITLE
mirrored the typed text produced ``integrity=mismatch(0/4) ok=True
outcome='verified' confidence=0.9`` in one payload — a success claim resting on the
weakest evidence in the pipeline while the strongest evidence in the same payload
said the text never landed (decision D-2a: a strong, target-scoped read-back
outranks a title/other-control substring match, so ``mismatch`` is NOT ``verified``).

**Explicitly NOT in scope, and pinned as a regression here:** the
``integrity=unverified`` + ``verified`` co-occurrence.  That split is intentional —
"no read-back was possible" is an absence of evidence about the focused control, and
the title/UI-control tier is still a legitimate, non-contradicted answer about the
needle being findable somewhere.  Only a *disagreement* (``mismatch``) outranks it.
"""

from __future__ import annotations

import asyncio
import base64
import io
from types import SimpleNamespace
from typing import Any

from PIL import Image

from computer_use_mcp.pipeline.agent import ComputerUseAgent
from computer_use_mcp.backend import FakeComputerBackend
from computer_use_mcp.core.limits import Limits
from computer_use_mcp.core.models import GroundedAction, Observation, Point, WindowInfo
from computer_use_mcp.security.safety import SafetyPolicy
from computer_use_mcp.core.state import StopToken, TaskState

WIDTH = 1280
HEIGHT = 720
TYPED = "1234"

LOGIN_WINDOW = WindowInfo(
    hwnd=777,
    pid=555,
    process_name="login.exe",
    exe_path="C:\\Windows\\login.exe",
    window_class="LoginWnd",
    title="Sign in",
    bounds=(0, 0, WIDTH, HEIGHT),
)


def _png() -> str:
    buffer = io.BytesIO()
    Image.new("RGB", (WIDTH, HEIGHT), (250, 250, 250)).save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


class _TypeWorld(FakeComputerBackend):
    """A login dialog whose read-back models the R-04 focused-control reader."""

    def __init__(self, *, title: str, focused_control_value: str | None) -> None:
        super().__init__(
            width=WIDTH,
            height=HEIGHT,
            active_window=WindowInfo(**{**LOGIN_WINDOW.model_dump(), "title": title}),
        )
        self.focused_control_value = focused_control_value

    def observe(self, monitor_index: int | None = None) -> Observation:
        observation = super().observe(monitor_index)
        observation.image_base64 = _png()
        return observation


def _agent(backend: FakeComputerBackend) -> ComputerUseAgent:
    return ComputerUseAgent(
        backend,
        provider=None,
        safety=SafetyPolicy(),
        task=TaskState(),
        stop=StopToken(),
        limits=Limits(
            min_screenshot_interval_ms=1, max_actions=100, max_task_seconds=600.0
        ).validate(),
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


def _type(*, title: str, focused_control_value: str | None) -> dict[str, Any]:
    backend = _TypeWorld(title=title, focused_control_value=focused_control_value)
    outcome = asyncio.run(
        _agent(backend).run_single(
            _state(), GroundedAction(action="type", text=TYPED, confidence=1.0), approved=True
        )
    )
    assert outcome.result is not None
    payload = outcome.result.model_dump()
    payload["_captures"] = len(backend.observed)
    return payload


def _integrity_status(message: str) -> str | None:
    """The bare status token out of an ``integrity=verified(4/4)`` style suffix."""
    if "integrity=" not in message:
        return None
    return message.split("integrity=", 1)[1].split()[0].split("(")[0]


# --- the defect: mismatch + a title match must NOT be verified -----------------------------------


def test_mismatch_read_back_with_a_mirroring_title_is_not_verified() -> None:
    """THE REPRODUCED CASE. A masked PIN field (read-back ``mismatch``) plus a window
    title that mirrors the typed account used to yield ``ok=True / verified`` at
    confidence 0.9 from the title substring match."""
    payload = _type(
        title=f"Sign in - account {TYPED}",
        focused_control_value="••••",  # masked -> mismatch
    )
    assert _integrity_status(payload["message"]) == "mismatch", payload["message"]
    assert payload["verification"]["outcome"] != "verified", payload["verification"]
    assert payload["verification"]["verified"] is False
    assert payload["ok"] is False
    # the demotion keeps the tier it came from, so the driver can see WHAT it was
    assert "ui_control_text" in payload["verification"]["verification_method"]
    # ... and it says WHY, on the additive evidence channel
    evidence = " | ".join(payload["verification"]["evidence"])
    assert "mismatch" in evidence, payload["verification"]["evidence"]
    # ... and never asserts the effect out loud
    assert "Expected text" not in payload["verification"]["note"], payload["verification"]


def test_mismatch_demotion_is_annotated_in_the_note_too() -> None:
    """The evidence line is the machine channel; the note names the blocker for a
    driver that only reads prose."""
    payload = _type(
        title=f"Sign in - account {TYPED}", focused_control_value="••••"
    )
    note = payload["verification"]["note"]
    assert "integrity" in note and "mismatch" in note, note
    assert payload["verification"]["confidence"] <= 0.5, payload["verification"]


# --- the controls -------------------------------------------------------------------------------


def test_unreadable_control_with_no_title_evidence_is_still_uncertain() -> None:
    """The A3 control, unchanged: no read-back AND no title evidence -> ``uncertain``."""
    payload = _type(title="Sign in", focused_control_value=None)
    assert _integrity_status(payload["message"]) == "unverified", payload["message"]
    assert payload["verification"]["outcome"] == "uncertain"
    assert payload["ok"] is False


def test_unverified_plus_verified_still_occurs_and_is_annotated() -> None:
    """THE REGRESSION PIN, and the explicit out-of-scope carve-out.

    ``integrity=unverified`` means the focused control could not be READ — an absence
    of evidence about that control, NOT a disagreement.  A window title that carries
    the needle is still a legitimate, uncontradicted answer, so the
    ``unverified`` + ``verified`` pairing must keep working exactly as before this
    fix; only the evidence channel gains the status line.
    """
    payload = _type(
        title=f"Sign in - account {TYPED}",
        focused_control_value=None,  # read-back impossible -> unverified
    )
    assert _integrity_status(payload["message"]) == "unverified", payload["message"]
    assert payload["verification"]["outcome"] == "verified", payload["verification"]
    assert payload["ok"] is True
    # the split is ANNOTATED, not silent: the evidence names the read-back status
    evidence = " | ".join(payload["verification"]["evidence"])
    assert "integrity" in evidence and "unverified" in evidence, evidence


def test_a_genuine_landing_still_verifies() -> None:
    """The ceiling: when the focused control's read-back agrees (``integrity=verified``)
    AND the title carries the needle, the action is still ``verified``."""
    payload = _type(
        title=f"Sign in - account {TYPED}",
        focused_control_value=TYPED,  # the read-back confirms the landing
    )
    assert _integrity_status(payload["message"]) == "verified", payload["message"]
    assert payload["verification"]["outcome"] == "verified", payload["verification"]
    assert payload["ok"] is True
    assert payload["verification"]["confidence"] > 0.5


def test_a_genuine_landing_with_a_quiet_title_is_not_claimed_by_the_weak_tier() -> None:
    """The other genuine landing: the read-back agrees but the title does NOT carry the
    needle.  The weak title/control tier abstains and the pixel tier cannot judge an
    unstated effect, so the verdict is the ladder's honest ``uncertain`` — the R-04
    status alone is NOT a positive verdict, and F2-1 does not make it one."""
    payload = _type(title="Sign in", focused_control_value=TYPED)
    assert _integrity_status(payload["message"]) == "verified", payload["message"]
    assert payload["verification"]["outcome"] == "uncertain", payload["verification"]
    assert payload["ok"] is False


def test_a_mismatch_type_with_no_title_evidence_is_uncertain_not_failed() -> None:
    """``mismatch`` is a disagreement about the FOCUSED CONTROL, not proof that nothing
    happened anywhere; with no title evidence the verdict is the ladder's honest
    abstention, not a hard failure."""
    payload = _type(title="Sign in", focused_control_value="••••")
    assert _integrity_status(payload["message"]) == "mismatch", payload["message"]
    assert payload["verification"]["outcome"] == "uncertain", payload["verification"]
    assert payload["ok"] is False


def test_a_non_type_action_is_untouched() -> None:
    """The demotion is TYPE-scoped: an ordinary click never consults the R-04 status."""
    backend = _TypeWorld(title="Sign in - account 1234", focused_control_value="••••")
    outcome = asyncio.run(
        _agent(backend).run_single(
            _state(),
            GroundedAction(action="click", point=Point(x=100, y=100), confidence=1.0),
            approved=True,
        )
    )
    assert outcome.result is not None
    assert _integrity_status(outcome.result.message) is None
