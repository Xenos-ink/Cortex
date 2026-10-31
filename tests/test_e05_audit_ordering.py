"""E-05 (ORVEX-2026-106, fixed during the WP4b port) — the audit verdict IS the reply
verdict.

Every demotion that can overturn a ladder ``verified`` — the R-04 type-integrity
mismatch veto, the ensure_app launch-outcome demotion, and the post-action
FOCUS_DRIFTED demotion — now runs BEFORE the single ``verification`` audit event is
emitted, so the audit row and the reply carry the SAME verdict on every executed-action
path. The drift demotion was the last exception (VAL-WP4a F2): it used to run after
``_record_verification``, leaving an audit row that said ``verified`` beside a reply
that said ``failed``. The ensure_app probe branch previously emitted NO verification
audit event at all; it now emits exactly one, from the same verdict the reply carries.
"""

from __future__ import annotations

from typing import Any

import pytest

from reply_contract import validate_reply
from test_controller_integration import (
    FAST_LIMITS,
    ScriptedBackend,
    audit_events,
    execute_payload,
    make_session,
)

from computer_use_mcp import server
from computer_use_mcp.core.models import WindowInfo


@pytest.fixture
def fresh_server(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Any:
    monkeypatch.setenv("COMPUTER_USE_MCP_LOG_DIR", str(tmp_path / "audit"))
    from computer_use_mcp.core.state import SessionRegistry

    monkeypatch.setattr(server, "_registry", SessionRegistry(max_sessions=8))
    monkeypatch.setattr(server, "_bundles", {})
    yield server


class _MaskedPinWorld(ScriptedBackend):
    """The login dialog whose focused-control read-back models the R-04 reader
    (tests/test_a3_mismatch_blocks_verified.py world, on the server surface)."""

    def __init__(self, *, title: str, focused_control_value: str | None) -> None:
        super().__init__(
            flip=False,
            active_window=WindowInfo(
                hwnd=777, pid=555, process_name="login.exe", window_class="LoginWnd",
                title=title, bounds=(0, 0, 1280, 720),
            ),
        )
        self.focused_control_value = focused_control_value

    def observe(self) -> Any:
        observation = super().observe()
        import base64 as _b64
        import io as _io

        from PIL import Image as _Image

        image = _Image.new("RGB", (1280, 720), (250, 250, 250))
        buffer = _io.BytesIO()
        image.save(buffer, format="PNG")
        observation.image_base64 = _b64.b64encode(buffer.getvalue()).decode("ascii")
        return observation


async def test_mismatch_demotion_audit_verdict_equals_the_reply_verdict(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """E-05, case 1 (mismatch veto): a masked PIN field plus a title that mirrors the
    typed text vetoes ``verified`` down to ``uncertain`` BEFORE the record — exactly
    one verification audit event, and its ``result`` equals the reply's
    ``verification.outcome``."""
    backend = _MaskedPinWorld(title="Sign in - account 1234", focused_control_value="••••")
    session_id, bundle, _backend, _ = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    payload = execute_payload(
        await server.computer_execute(session_id, "type", text="1234", approved=True)
    )
    validate_reply(payload, "flat_execute")
    assert payload["result"] == "uncertain"
    assert payload["verification"]["outcome"] == "uncertain"
    verification_events = [
        e for e in audit_events(bundle, session_id) if e["event_type"] == "verification"
    ]
    assert len(verification_events) == 1, verification_events
    assert verification_events[0]["result"] == payload["verification"]["outcome"] == "uncertain"


class _DriftAfterVerifiedType(ScriptedBackend):
    """The type lands (and IS provable), THEN focus drifts to a foreign window — the
    post-action FOCUS_DRIFTED abort that used to demote AFTER the audit record."""

    def __init__(self) -> None:
        super().__init__(
            flip=False,
            active_window=WindowInfo(
                hwnd=1, pid=10, process_name="login.exe", window_class="LoginWnd",
                title="Sign in - typed hello", bounds=(0, 0, 1280, 720),
            ),
        )
        self.execute_calls = 0
        self.focused_control_value = ""

    def execute(self, action: Any, stop: Any = None, **kwargs: Any) -> str:
        self.execute_calls += 1
        if self.execute_calls == 2:
            # The text is genuinely readable where it landed (the ladder can verify)
            # — but by the time the post-action guard looks, focus is elsewhere.
            self.focused_control_value = "typed hello"
            self.focus_target = {
                "hwnd_focus": 5, "root_hwnd": 999, "window_class": "Edit",
                "text": "", "pid": 1,
            }
        return super().execute(action, stop)

    def observe(self) -> Any:
        observation = super().observe()
        import base64 as _b64
        import io as _io

        from PIL import Image as _Image

        image = _Image.new("RGB", (1280, 720), (250, 250, 250))
        buffer = _io.BytesIO()
        image.save(buffer, format="PNG")
        observation.image_base64 = _b64.b64encode(buffer.getvalue()).decode("ascii")
        return observation


async def test_drift_demotion_audit_verdict_equals_the_reply_verdict(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """E-05, case 2 (the VAL-WP4a F2 hand-over): a post-action FOCUS_DRIFTED abort
    demotes ``verified`` to ``failed`` AFTER the ladder — the audit event now records
    the FINAL verdict, so the audit row says ``failed`` exactly like the reply."""
    backend = _DriftAfterVerifiedType()
    session_id, bundle, _backend, _ = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    # Bind the guard to the session's own window first (the anchor the type then
    # drifts AWAY from — the same arming test_focus_continuity does with rebind).
    focus_payload = execute_payload(
        await server.computer_execute(session_id, "focus_window", target="Sign in")
    )
    assert focus_payload["ok"] is True, focus_payload
    payload = execute_payload(
        await server.computer_execute(session_id, "type", text="hello", approved=True)
    )
    validate_reply(payload, "flat_execute")
    # The drift demotion is an honest not-ok (the text may have landed elsewhere)...
    assert payload["result"] == "failed", payload
    assert payload["verification"]["outcome"] == "failed"
    assert payload["verification"]["verification_method"] == "interference_post_action"
    # ...and the type action's audit row carries the SAME final verdict — before E-05
    # this row said ``verified`` with the ladder's method, and NO
    # ``interference_post_action`` row existed at all (the drift demotion ran after
    # the record). The focus_window arming action above has its own ``verified``
    # event, so discriminate by the demotion's verification method.
    demoted_events = [
        e
        for e in audit_events(bundle, session_id)
        if e["event_type"] == "verification"
        and e.get("metadata", {}).get("method") == "interference_post_action"
    ]
    assert len(demoted_events) == 1, demoted_events
    assert demoted_events[0]["result"] == "failed", demoted_events[0]
    assert demoted_events[0]["result"] == payload["verification"]["outcome"]


async def test_launch_failure_audit_verdict_equals_the_reply_verdict(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """E-05, case 3 (launch-outcome demotion / probe audit event): a launch failure is
    demoted to ``failed`` in the probe branch itself, and the probe now emits exactly
    ONE verification audit event carrying that same ``failed`` verdict (the dev copy's
    probe branch emitted none)."""
    from computer_use_mcp.backend import FakeComputerBackend

    backend = FakeComputerBackend()  # no app windows: NO_INSTANCE everywhere
    backend.launch_unresolvable = {"mspaint.exe"}
    session_id, bundle, _backend, _ = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    payload = execute_payload(
        await server.computer_execute(session_id, "ensure_app", target="mspaint.exe")
    )
    validate_reply(payload, "flat_execute")
    assert payload["result"] == "failed", payload
    assert payload["ensure_app_outcome"] == "launch_unresolved"
    assert payload["ok"] is False
    verification_events = [
        e for e in audit_events(bundle, session_id) if e["event_type"] == "verification"
    ]
    assert len(verification_events) == 1, verification_events
    assert verification_events[0]["result"] == "failed", verification_events[0]
    assert verification_events[0]["result"] == payload["verification"]["outcome"]
    assert verification_events[0]["metadata"]["ladder_tier"] == "ensure_app_probe"


async def test_marker_audit_event_still_exactly_one_and_equal(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """E-05 (WP4b): the unified record keeps the marker path at exactly ONE
    verification audit event (``no_op_marker`` tier), carrying the reply's own
    ``verified`` outcome."""
    backend = ScriptedBackend(flip=False)
    session_id, bundle, _backend, _ = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    payload = execute_payload(await server.computer_execute(session_id, "done"))
    validate_reply(payload, "flat_execute")
    assert payload["result"] == "marker" and payload["ok"] is True
    verification_events = [
        e for e in audit_events(bundle, session_id) if e["event_type"] == "verification"
    ]
    assert len(verification_events) == 1, verification_events
    assert verification_events[0]["result"] == "verified", verification_events[0]
    assert verification_events[0]["metadata"]["ladder_tier"] == "no_op_marker"


@pytest.mark.parametrize("action", ["done", "wait"])
async def test_done_and_wait_markers_each_emit_the_verification_audit_event(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    """The done/wait markers keep their audit trail through the unified record (E-05:
    'restore the done/wait/ensure probe verification audit events') — one event per
    action, result ``verified``, tier ``no_op_marker``."""
    backend = ScriptedBackend(flip=False)
    session_id, bundle, _backend, _ = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    payload = execute_payload(await server.computer_execute(session_id, action))
    validate_reply(payload, "flat_execute")
    assert payload["result"] == "marker"
    events = [
        e for e in audit_events(bundle, session_id) if e["event_type"] == "verification"
    ]
    assert len(events) == 1, events
    assert events[0]["result"] == "verified"
    assert events[0]["metadata"]["ladder_tier"] == "no_op_marker"
