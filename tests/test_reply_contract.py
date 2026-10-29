"""WP1 (ORVEX-2026-108) — the unified reply contract, validated on REAL replies.

Every test here drives a real reply through the server tool functions on the fake
backend (the ``test_controller_integration`` patterns) and then runs it through the
WP0 normative helper :func:`reply_contract.validate_reply` with the correct surface.
No hand-built reply dicts are asserted — the shapes tested are the shapes the server
actually emits.

Covered §1 surfaces / situations:

- flat execute: verified / uncertain / failed / rejected / approval-gated /
  dry_run / done marker / wait marker;
- follow_up item: the queue entry envelope (result token + shared ok-rule);
- rejection: the boundary ``invalid_action`` rejection envelope (no ``error`` key);
- mid-type focus drift: the K-05 ``partial_delivery.delivered_chars`` (present after
  ≥1 delivered chunk, absent before any), plus the chunked-type counting unit on the
  real backend's dispatch loop;
- ensure_app: the typed ``ensure_app_outcome`` enum (S-12);
- observe: the image-bearing reply carries the single measured scale key.
"""

from __future__ import annotations

import platform
from typing import Any

import pytest
from reply_contract import (
    assert_approval_gate,
    assert_dry_run_reply,
    assert_image_scale_keys,
    assert_marker_reply,
    assert_no_compat_flags,
    assert_ok_rule,
    assert_partial_delivery,
    assert_rejection,
    assert_result_token,
    validate_reply,
)
from test_controller_integration import (
    FAST_LIMITS,
    ScriptedBackend,
    audit_events,
    execute_payload,
    executed_summary,
    make_session,
)

from computer_use_mcp import server
from computer_use_mcp.backend import FakeComputerBackend, FocusDriftError
from computer_use_mcp.models import WindowInfo


@pytest.fixture
def fresh_server(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Any:
    monkeypatch.setenv("COMPUTER_USE_MCP_LOG_DIR", str(tmp_path / "audit"))
    from computer_use_mcp.state import SessionRegistry

    monkeypatch.setattr(server, "_registry", SessionRegistry(max_sessions=8))
    monkeypatch.setattr(server, "_bundles", {})
    yield server


# --- flat execute: the verdict trio ---------------------------------------------------------------


async def test_flat_verified_reply(fresh_server: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    backend = ScriptedBackend(flip=True)  # every dispatch flips pixels -> provable change
    session_id, _bundle, backend, _ = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    payload = execute_payload(
        await server.computer_execute(session_id, "click", x=10, y=10, approved=True)
    )
    validate_reply(payload, "flat_execute")
    assert payload["result"] == "verified"
    assert payload["ok"] is True
    assert payload["verification"]["outcome"] == "verified"


async def test_flat_uncertain_reply(fresh_server: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    backend = ScriptedBackend(flip=False)  # static screen: pixels cannot prove a change
    session_id, _bundle, backend, _ = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    payload = execute_payload(
        await server.computer_execute(session_id, "click", x=10, y=10, approved=True)
    )
    validate_reply(payload, "flat_execute")
    assert payload["result"] == "uncertain"
    assert payload["ok"] is False  # uncertain is never success (§1.1 ok-rule)
    assert payload["verification"]["outcome"] == "uncertain"


async def test_flat_failed_reply(fresh_server: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """A deterministic expectation that provably did not happen is ``failed``."""
    backend = ScriptedBackend(
        flip=False,
        active_window=WindowInfo(hwnd=1, pid=10, process_name="app.exe", title="App"),
    )
    session_id, _bundle, backend, _ = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    payload = execute_payload(
        await server.computer_execute(
            session_id, "keypress", keys=["enter"], approved=True, expected_effect="open Calculator"
        )
    )
    validate_reply(payload, "flat_execute")
    assert payload["result"] == "failed"
    assert payload["ok"] is False
    assert payload["verification"]["outcome"] == "failed"


# --- follow_up item: identical envelope ------------------------------------------------------------


async def test_follow_up_item_envelope(fresh_server: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    backend = ScriptedBackend(flip=True)
    session_id, _bundle, backend, _ = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    response = await server.computer_execute(
        session_id, "click", x=10, y=10, approved=True, include_screenshot_after=False,
        follow_ups=[{"action": "done"}],
    )
    payload = execute_payload(response)
    validate_reply(payload, "flat_execute")
    entries = payload["follow_up_results"]
    assert len(entries) == 2
    for entry in entries:
        validate_reply(entry, "follow_up_item")
    assert entries[0]["result"] == "verified" and entries[0]["ok"] is True
    assert entries[1]["result"] == "marker" and entries[1]["ok"] is True


# --- rejection: the §1.1 envelope -------------------------------------------------------------------


async def test_rejection_envelope(fresh_server: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """A malformed follow-up item is refused at the boundary with the rejection
    envelope — reasons, never an ``error`` key."""
    session_id, _bundle, _backend, _ = make_session(
        monkeypatch, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    payload = await server.computer_execute(
        session_id, "click", x=10, y=10, follow_ups=[{"action": "focus_window"}]
    )
    validate_reply(payload, "flat_execute")
    assert_rejection(payload)
    assert "invalid_action" in payload["reasons"]


async def test_validator_rejection_envelope(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A gate refusal (allowlist identity unavailable) is ``rejected`` with reasons."""
    session_id, _bundle, _backend, _ = make_session(
        monkeypatch,
        dry_run=False,
        require_approval=False,
        limits=FAST_LIMITS,
        allowed_processes=["excel.exe"],
    )
    payload = await server.computer_execute(session_id, "click", x=10, y=10, approved=True)
    validate_reply(payload, "flat_execute")
    assert_rejection(payload)


# --- mid-type focus drift: K-05 partial_delivery ----------------------------------------------------


class DriftOnTypeBackend(ScriptedBackend):
    """The T8 mid-type drift, simulated at the backend seam: the focus guard aborts
    the in-flight type after a delivered prefix (the ``delivered_chars`` the real
    chunked dispatcher attaches — pinned against the real loop below)."""

    def __init__(self, *, delivered_chars: int | None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._drift_delivered = delivered_chars

    def execute(
        self, action: Any, stop: Any = None, focus_hook: Any = None, allow_launch: bool = False
    ) -> str:
        if action.action.value == "type" and action.text:
            exc = FocusDriftError(
                "FOCUS_DRIFTED the focused control stopped belonging to the bound window"
            )
            exc.delivered_chars = self._drift_delivered
            raise exc
        return super().execute(action, stop)


async def test_mid_type_drift_rejection_carries_partial_delivery(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = DriftOnTypeBackend(delivered_chars=7, flip=False)
    session_id, _bundle, backend, _ = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    payload = await server.computer_execute(
        session_id, "type", text="hello world", approved=True
    )
    validate_reply(payload, "flat_execute")
    assert_rejection(payload)
    assert_partial_delivery(payload)
    assert payload["partial_delivery"]["delivered_chars"] == 7


async def test_drift_before_any_chunk_has_no_partial_delivery_key(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Absence of the key means "nothing dispatched" (§1.1 K-05)."""
    backend = DriftOnTypeBackend(delivered_chars=None, flip=False)
    session_id, _bundle, backend, _ = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    payload = await server.computer_execute(
        session_id, "type", text="hello world", approved=True
    )
    validate_reply(payload, "flat_execute")
    assert_rejection(payload)
    assert "partial_delivery" not in payload


def test_chunked_type_counts_delivered_chars_before_the_abort(real_backend: Any) -> None:
    """The REAL chunked dispatcher (stubbed engine, no real input): the abort on chunk
    N records exactly the characters of chunks 0..N-1 (§1.1 K-05's source of truth)."""
    if platform.system() != "Windows":
        pytest.skip("Real Win32 backend requires Windows.")
    from computer_use_mcp.backend import TYPE_CHUNK_CHARS
    from computer_use_mcp.models import GroundedAction

    backend = real_backend
    chunks_seen: list[str] = []

    class DriftEngine:
        def type_text(self, chunk: str, before_chunk: Any = None) -> None:
            chunks_seen.append(chunk)
            if len(chunks_seen) == 3:  # the guard aborts before chunk 3 dispatches
                raise FocusDriftError("FOCUS_DRIFTED mid-chunk")

    import computer_use_mcp.backend as backend_module

    monkeypatched = pytest.MonkeyPatch()
    try:
        monkeypatched.setattr(backend, "_engine", DriftEngine())
        monkeypatched.setattr(backend, "_apply_focus_settle", lambda: None)
        action = GroundedAction(action="type", text="A" * (TYPE_CHUNK_CHARS * 4), confidence=1.0)
        with pytest.raises(FocusDriftError) as excinfo:
            backend._execute_type(action, None, None)
        assert excinfo.value.delivered_chars == TYPE_CHUNK_CHARS * 2
        assert len(chunks_seen) == 3  # the aborting chunk itself contributed nothing
    finally:
        monkeypatched.undo()


# --- approval gate -----------------------------------------------------------------------------------


async def test_approval_gated_reply(fresh_server: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    session_id, _bundle, backend, _ = make_session(
        monkeypatch, dry_run=False, require_approval=True, limits=FAST_LIMITS
    )
    payload = await server.computer_execute(session_id, "click", x=10, y=10)
    assert_approval_gate(payload)
    assert backend.executed == []  # nothing dispatched while approval is pending


# --- dry_run + markers --------------------------------------------------------------------------------


async def test_dry_run_reply(fresh_server: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    session_id, _bundle, backend, _ = make_session(
        monkeypatch, dry_run=True, require_approval=False, limits=FAST_LIMITS
    )
    payload = execute_payload(await server.computer_execute(session_id, "click", x=10, y=10))
    validate_reply(payload, "flat_execute")
    assert_dry_run_reply(payload)
    assert payload["dry_run"] is True
    assert backend.executed == []


async def test_done_marker_reply(fresh_server: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    session_id, _bundle, _backend, _ = make_session(
        monkeypatch,
        backend=ScriptedBackend(flip=False),
        dry_run=False,
        require_approval=False,
        limits=FAST_LIMITS,
    )
    payload = execute_payload(await server.computer_execute(session_id, "done"))
    validate_reply(payload, "flat_execute")
    assert_marker_reply(payload)


async def test_wait_marker_reply(fresh_server: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    session_id, _bundle, _backend, _ = make_session(
        monkeypatch,
        backend=ScriptedBackend(flip=False),
        dry_run=False,
        require_approval=False,
        limits=FAST_LIMITS,
    )
    payload = execute_payload(await server.computer_execute(session_id, "wait", delta=0))
    validate_reply(payload, "flat_execute")
    assert_marker_reply(payload)


# --- ensure_app typed outcome (S-12) ------------------------------------------------------------------


async def test_ensure_app_typed_outcome(fresh_server: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from computer_use_mcp.backend import AppWindowCandidate, is_unsaved_candidate_title

    book = WindowInfo(
        hwnd=1, pid=4242, process_name="excel", window_class="FakeWnd",
        title="Book1 - Excel", bounds=(0, 0, 1280, 720),
    )
    backend = FakeComputerBackend()
    backend.set_windows([book])
    backend.app_windows = [
        AppWindowCandidate(
            window=book,
            doc_token="book1",
            unsaved_candidate=is_unsaved_candidate_title(book.title),
        )
    ]
    session_id, _bundle, _backend, _ = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS,
        allowed_processes=["excel.exe"],
    )
    payload = execute_payload(
        await server.computer_execute(session_id, "ensure_app", target="excel|book1", approved=True)
    )
    validate_reply(payload, "flat_execute")
    assert payload["ensure_app_outcome"] == "reattached"
    assert_result_token(payload, surface="flat_execute")


# --- observe: the image-bearing reply -----------------------------------------------------------------


def test_observe_image_reply_contract(fresh_server: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    session_id, _bundle, _backend, _ = make_session(
        monkeypatch, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    import json as _json

    blocks = server.computer_observe(session_id)
    metadata = _json.loads(blocks[0].text)
    validate_reply(metadata, "observe")
    scale = assert_image_scale_keys(metadata, image_required=True)
    assert scale is None or 0.0 < scale < 1.0  # MINOR-b: pass-through None or a downscale
    assert_no_compat_flags(metadata)
    assert_ok_rule_is_not_required_on_read_only(metadata)


def assert_ok_rule_is_not_required_on_read_only(metadata: dict[str, Any]) -> None:
    """Read-only surfaces carry no ``result`` token (§1.3): the ok-rule has no input."""
    assert "result" not in metadata
    assert metadata.get("ok") is not None


# --- WP1 delta: the four remaining execute-surface reply paths -----------------------------------


async def test_limit_trip_reply_is_a_rejection(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A limit trip is a GATE REFUSAL (verified pre-dispatch: the observation-phase
    rate ceiling and ``check_action`` both fire before the triggering action
    dispatches input): rejection envelope, typed limit as the first reason, the
    additive ``limit`` key kept, no ``error`` key."""
    session_id, _bundle, backend, _ = make_session(
        monkeypatch, dry_run=False, require_approval=False,
        limits={"min_screenshot_interval_ms": 60_000},
    )
    first = execute_payload(await server.computer_execute(session_id, "click", x=10, y=10))
    assert first["ok"] is True
    payload = await server.computer_execute(session_id, "click", x=20, y=20)
    validate_reply(payload, "flat_execute")
    assert_rejection(payload)
    assert payload["reasons"] == ["limit_exceeded: min_screenshot_interval_ms"]
    assert payload["limit"] == "min_screenshot_interval_ms"
    assert len(backend.executed) == 1  # the over-budget action executed NOTHING


async def test_unknown_session_execute_reply_is_a_rejection(fresh_server: Any) -> None:
    """Execute-surface lifecycle refusals are rejections (§1.1); the read-only tools
    keep their typed ``_error_response`` shapes (§1.3)."""
    payload = await server.computer_execute("does-not-exist", "wait", delta=0)
    validate_reply(payload, "flat_execute")
    assert_rejection(payload)
    assert payload["reasons"] == ["unknown_session"]
    assert payload["session_id"] == "does-not-exist"


async def test_stopped_session_execute_reply_is_a_rejection(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id, _bundle, _backend, _ = make_session(
        monkeypatch, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    server.stop_session(session_id)
    payload = await server.computer_execute(session_id, "wait", delta=0)
    validate_reply(payload, "flat_execute")
    assert_rejection(payload)
    assert payload["reasons"] == ["session_stopped"]
    assert payload["session_id"] == session_id


async def test_invalid_visual_view_on_execute_is_a_rejection(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The malformed-parameter refusal of an ACTION call is a rejection (the typed
    code names itself as the first reason); observe/zoom keep the typed error."""
    session_id, _bundle, backend, _ = make_session(
        monkeypatch, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    payload = await server.computer_execute(session_id, "click", x=10, y=10, visual_view="3d")
    validate_reply(payload, "flat_execute")
    assert_rejection(payload)
    assert payload["reasons"][0].startswith("invalid_visual_view")
    assert backend.executed == []  # fail-closed BEFORE anything ran


async def test_defensive_no_result_branch_is_failed_action_error(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The defensive branch (an executed outcome that somehow carries no result — no
    real pipeline path reaches it) is a ``failed`` action_error reply. Unit-level
    shape check: no verification block exists because nothing was dispatched, so the
    full validate_reply verification requirement does not apply here."""
    from computer_use_mcp.agent import SingleActionOutcome

    session_id, bundle, _backend, _ = make_session(
        monkeypatch, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )

    async def _empty_outcome(*args: Any, **kwargs: Any) -> SingleActionOutcome:
        return SingleActionOutcome(kind="executed", result=None)

    monkeypatch.setattr(bundle.agent, "run_single", _empty_outcome)
    payload = await server.computer_execute(session_id, "click", x=10, y=10)
    assert payload["result"] == "failed"
    assert_ok_rule(payload)
    assert payload["error"] == "action_error"
    assert payload["message"]


async def test_no_execute_rejection_reply_ever_carries_an_error_key(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§1.1/§1.4 (e): rejected replies carry reasons, NEVER an ``error`` key —
    parametrized over every rejection path this suite can drive through the real
    ``computer_execute`` surface."""
    # 1. invalid action vocabulary (boundary teaching rejection)
    session_id, _bundle, _backend, _ = make_session(
        monkeypatch, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    # a session whose process allowlist cannot be verified -> validator rejection
    session_id_allow, _b_allow, _bk_allow, _ = make_session(
        monkeypatch, dry_run=False, require_approval=False, limits=FAST_LIMITS,
        allowed_processes=["excel.exe"],
    )
    rejected_replies = [
        await server.computer_execute(session_id, "teleport"),
        # 2. malformed follow-up item (boundary teaching rejection)
        await server.computer_execute(session_id, "click", x=1, y=1,
                                      follow_ups=[{"action": "focus_window"}]),
        # 3. validator rejection (allowlist identity unavailable)
        await server.computer_execute(session_id_allow, "click", x=2, y=2),
        # 4. invalid visual_view parameter
        await server.computer_execute(session_id, "click", x=3, y=3, visual_view="3d"),
    ]
    # 5. unknown session 6. stopped session
    rejected_replies.append(await server.computer_execute("does-not-exist", "wait", delta=0))
    session_id2, _bundle2, _backend2, _ = make_session(
        monkeypatch, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    server.stop_session(session_id2)
    rejected_replies.append(await server.computer_execute(session_id2, "wait", delta=0))
    for payload in rejected_replies:
        assert payload["result"] == "rejected", payload
        assert "error" not in payload, payload
        assert payload["reasons"], payload


def test_chunked_type_counts_per_character_drift(real_backend: Any) -> None:
    """MAJOR-3 (VAL-WP1): the REAL pyautogui engine (fake pyautogui module) calls the
    focus hook PER CHARACTER, and a drift at probe 30 of the first 64-char chunk
    reports the exact dispatched prefix (29) through the real chunk loop."""
    if platform.system() != "Windows":
        pytest.skip("Real Win32 backend requires Windows.")
    from computer_use_mcp.backend import TYPE_CHUNK_CHARS, PyAutoGuiInputEngine
    from computer_use_mcp.models import GroundedAction

    backend = real_backend

    class FakePA:
        FailSafeException = type("FailSafeException", (Exception,), {})

        def write(self, char: str) -> None:
            pass

    probes = {"n": 0}

    def focus_hook() -> None:
        probes["n"] += 1
        if probes["n"] == 30:  # the guard aborts INSTEAD of the 30th probe returning
            raise FocusDriftError("FOCUS_DRIFTED mid-chunk")

    patch = pytest.MonkeyPatch()
    try:
        patch.setattr(backend, "_engine", PyAutoGuiInputEngine(FakePA(), type_interval=0.0))
        patch.setattr(backend, "_apply_focus_settle", lambda: None)
        action = GroundedAction(action="type", text="A" * (TYPE_CHUNK_CHARS * 2), confidence=1.0)
        with pytest.raises(FocusDriftError) as excinfo:
            backend._execute_type(action, None, focus_hook)
        assert excinfo.value.delivered_chars == 29, excinfo.value.delivered_chars
    finally:
        patch.undo()


def test_integrity_off_path_counts_engine_dispatched_prefix(real_backend: Any) -> None:
    """MAJOR-3: the integrity-OFF single-call path composes the engine's own
    per-character count (no outer chunk arithmetic to hide behind)."""
    if platform.system() != "Windows":
        pytest.skip("Real Win32 backend requires Windows.")
    import computer_use_mcp.backend as backend_module
    from computer_use_mcp.backend import PyAutoGuiInputEngine
    from computer_use_mcp.models import GroundedAction

    backend = real_backend

    class FakePA2:
        FailSafeException = type("FailSafeException", (Exception,), {})

        def write(self, char: str) -> None:
            pass

    probes = {"n": 0}

    def focus_hook() -> None:
        probes["n"] += 1
        if probes["n"] == 13:
            raise FocusDriftError("FOCUS_DRIFTED")

    patch = pytest.MonkeyPatch()
    try:
        patch.setattr(backend, "_engine", PyAutoGuiInputEngine(FakePA2(), type_interval=0.0))
        patch.setattr(backend, "_apply_focus_settle", lambda: None)
        patch.setattr(backend_module, "TYPE_INTEGRITY_ENABLED", False)
        action = GroundedAction(action="type", text="B" * 40, confidence=1.0)
        with pytest.raises(FocusDriftError) as excinfo:
            backend._execute_type(action, None, focus_hook)
        assert excinfo.value.delivered_chars == 12, excinfo.value.delivered_chars
    finally:
        patch.undo()


def test_repair_retype_drift_counts_landed_prefix(real_backend: Any) -> None:
    """MAJOR-3: a drift abort during the ONE verified retype reports the ORIGINAL
    landed prefix (the characters already in the field) — never zero."""
    if platform.system() != "Windows":
        pytest.skip("Real Win32 backend requires Windows.")
    from computer_use_mcp.models import GroundedAction

    backend = real_backend
    calls = {"n": 0}

    class RepairDriftEngine:
        def type_text(self, chunk: str, before_chunk: Any = None) -> None:
            calls["n"] += 1
            if calls["n"] == 2:  # the verified RETYPE call aborts immediately
                exc = FocusDriftError("FOCUS_DRIFTED during retype")
                exc.delivered_chars = 0
                raise exc

    patch = pytest.MonkeyPatch()
    try:
        patch.setattr(backend, "_engine", RepairDriftEngine())
        patch.setattr(backend, "_apply_focus_settle", lambda: None)
        # the normalizer folds line endings only — the read must match the typed case
        patch.setattr(backend, "_read_landed_normalized", lambda: "C" * 20)
        action = GroundedAction(action="type", text="C" * 30, confidence=1.0)
        with pytest.raises(FocusDriftError) as excinfo:
            backend._execute_type(action, None, None)
        # N5 (VAL-WP1-02): the count is the UPPER BOUND len(text) — everything was
        # dispatched; the read-back-confirmed prefix (20) may under-report late drain.
        assert excinfo.value.delivered_chars == 30, excinfo.value.delivered_chars
    finally:
        patch.undo()


# --- DELTA 2 (VAL-WP1 findings): the remaining reply families, on real replies -----


class _AlternatingObserveBackend(ScriptedBackend):
    """Every observe() call returns a DIFFERENT digest (identity-less fake): the
    queued item's premise can never match its fresh validate capture."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(flip=False, **kwargs)
        self._obs_calls = 0

    def observe(self) -> Any:
        observation = super().observe()
        import base64 as _b64
        import io as _io

        from PIL import Image as _Image

        color = "white" if self._obs_calls % 2 == 0 else "black"
        self._obs_calls += 1
        image = _Image.new("RGB", (64, 48), color)
        buffer = _io.BytesIO()
        image.save(buffer, format="PNG")
        observation.image_base64 = _b64.b64encode(buffer.getvalue()).decode("ascii")
        return observation


async def test_safety_denied_flat_reply_is_a_named_rejection(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The safety gate refuses a destructive type — a rejection whose FIRST reason
    names the gate (the shared helper), validated through the contract."""
    session_id, _bundle, backend, _ = make_session(
        monkeypatch, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    payload = await server.computer_execute(
        session_id, "type", text="rm -rf C:/Windows && format C:", approved=True
    )
    validate_reply(payload, "flat_execute")
    assert_rejection(payload)
    assert payload["reasons"][0].startswith("safety_denied")
    assert backend.executed == []


async def test_safety_denied_queue_entry_envelope(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MAJOR-2: a safety-denied queue entry carries the identical field set as flat —
    result token + gate-named non-empty reasons (never [])."""
    session_id, _bundle, backend, _ = make_session(
        monkeypatch, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    payload = execute_payload(
        await server.computer_execute(
            session_id, "click", x=10, y=10, approved=True,
            follow_ups=[{"action": "type", "text": r"del C:\Windows\System32 /s /q",
                         "expected_effect": "x"}],
        )
    )
    validate_reply(payload, "flat_execute")
    entries = payload["follow_up_results"]
    assert len(entries) == 2 and entries[1]["kind"] == "safety_denied"
    validate_reply(entries[1], "follow_up_item")
    assert entries[1]["reasons"][0].startswith("safety_denied")
    assert payload["follow_ups_stopped_reason"] == "safety_denied"


async def test_digest_surprise_queue_entry_envelope(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MAJOR-2: a digest-surprised queue entry carries result + typed non-empty
    reasons (the shared helper), validated through the contract. Trigger: an
    observe-alternating fake screen (identity-less fake: the digest probe governs the
    queued item's premise, and every capture differs)."""
    backend = _AlternatingObserveBackend()
    session_id, _bundle, backend, _ = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    payload = execute_payload(
        await server.computer_execute(
            session_id, "click", x=10, y=10, approved=True,
            follow_ups=[{"action": "click", "x": 30, "y": 30}],
        )
    )
    validate_reply(payload, "flat_execute")
    entries = payload["follow_up_results"]
    assert len(entries) == 2 and entries[1]["kind"] == "digest_surprise"
    validate_reply(entries[1], "follow_up_item")
    assert entries[1]["result"] == "rejected"
    assert entries[1]["reasons"][0].startswith("digest_surprise")
    assert payload["follow_ups_stopped_reason"] == "digest_surprise"


async def test_operation_error_reply_is_failed_action_error(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A backend operation error is result ``failed`` (ok False); no verification
    block exists because nothing was dispatched — the §1 helper scopes verification
    to dispatched input actions."""
    from computer_use_mcp.models import GroundedAction

    class ExplodingScrollBackend(ScriptedBackend):
        def execute(
            self, action: Any, stop: Any = None, focus_hook: Any = None, allow_launch: bool = False
        ) -> str:
            if action.action.value == "scroll":
                raise RuntimeError("injected scroll failure")
            return super().execute(action, stop)

    session_id, _bundle, backend, _ = make_session(
        monkeypatch, backend=ExplodingScrollBackend(flip=True),
        dry_run=False, require_approval=False, limits=FAST_LIMITS,
    )
    payload = await server.computer_execute(session_id, "scroll", delta=1, approved=True)
    validate_reply(payload, "flat_execute")
    assert payload["result"] == "failed"
    assert payload["ok"] is False
    assert payload["error"] == "action_error"
    assert backend.executed == []


async def test_task_stopped_reply_carries_the_stopped_token(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A host-requested stop surfacing through ``computer_execute`` replies with the
    §1.2 ``stopped`` token (ok False by the shared ok-rule)."""
    from computer_use_mcp.agent import SingleActionOutcome  # noqa: F401 - shape reference
    from computer_use_mcp.state import TaskStopped

    session_id, bundle, _backend, _ = make_session(
        monkeypatch, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )

    async def _raise_stopped(*args: Any, **kwargs: Any) -> Any:
        raise TaskStopped("Stop requested; stopping before the next action.")

    monkeypatch.setattr(bundle.agent, "run_single", _raise_stopped)
    payload = await server.computer_execute(session_id, "click", x=10, y=10)
    validate_reply(payload, "flat_execute")
    assert payload["result"] == "stopped"
    assert payload["ok"] is False


async def test_mid_batch_limit_keeps_the_partial_history(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MAJOR-1: a limit trip on queue item 1 does NOT label the call "rejected, zero
    effect" — the flat reply stays item 0's executed reply, the entries for every
    item that ran (plus the tripped item's rejection entry) ride follow_up_results,
    and the stop reason names the limit."""
    session_id, _bundle, backend, _ = make_session(
        monkeypatch, dry_run=False, require_approval=False,
        limits={"min_screenshot_interval_ms": 0, "max_actions": 1},
    )
    payload = execute_payload(
        await server.computer_execute(
            session_id, "click", x=10, y=10, approved=True,
            follow_ups=[{"action": "click", "x": 30, "y": 30}],
        )
    )
    validate_reply(payload, "flat_execute")
    assert payload["result"] == "verified"  # item 0's OWN executed verdict
    assert payload["ok"] is True
    entries = payload["follow_up_results"]
    assert len(entries) == 2, entries
    for entry in entries:
        validate_reply(entry, "follow_up_item")
    assert entries[0]["result"] == "verified" and entries[0]["ok"] is True
    assert entries[1]["result"] == "rejected" and entries[1]["ok"] is False
    assert entries[1]["reasons"] == ["limit_exceeded: max_actions"]
    assert payload["follow_ups_stopped_reason"] == "limit_exceeded"
    assert len(backend.executed) == 1  # item 1 dispatched NOTHING


async def test_ensure_app_failure_outcomes_are_failed_or_uncertain(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MAJOR-4: not_found / launch_rejected reply result ``failed`` (ok False);
    ambiguous replies ``uncertain`` (ok False) — the §1.3 verdicts, on real replies."""
    from computer_use_mcp.backend import AppWindowCandidate

    async def _drive(
        world: Any, target: str, policy: str | None = None,
        allowlist: list[str] | None = None,
    ) -> dict[str, Any]:
        server._backend_factory = lambda: world
        server._provider_factory = lambda: _FakeProvider()
        interference = {"attach_or_launch": {"launch": policy}} if policy is not None else None
        response = server.start_session(
            dry_run=False, require_approval=False, limits=FAST_LIMITS,
            allowed_processes=allowlist if allowlist is not None else ["excel.exe", "mspaint.exe"],
            **({"interference": interference} if interference is not None else {}),
        )
        session_id = str(response["session_id"])
        try:
            return execute_payload(
                await server.computer_execute(session_id, "ensure_app", target=target)
            )
        finally:
            server.stop_session(session_id=session_id)

    async def _drive_with_allowlist(world: Any, target: str, allowlist: list[str]) -> dict[str, Any]:
        return await _drive(world, target, allowlist=allowlist)

    # an ACTIVE window is required so the validator can verify process identity
    # against the allowlist before the launch gate is ever consulted
    book = WindowInfo(hwnd=1, pid=42, process_name="excel", window_class="FakeWnd",
                      title="Book1 - Excel", bounds=(0, 0, 1280, 720))

    def _world() -> FakeComputerBackend:
        world = FakeComputerBackend()
        world.set_windows([book])
        return world

    not_found = await _drive(_world(), "mspaint", policy="driver")
    validate_reply(not_found, "flat_execute")
    assert not_found["ensure_app_outcome"] == "not_found"
    assert not_found["result"] == "failed" and not_found["ok"] is False

    # the needle must PASS the process allowlist to reach the launch validator,
    # where its charset rejects it (launch_rejected=LaunchTargetError)
    launch_rejected = await _drive(_world(), "ms;paint", allowlist=["excel.exe", "ms;paint"])
    validate_reply(launch_rejected, "flat_execute")
    assert launch_rejected["ensure_app_outcome"] == "launch_rejected"
    assert launch_rejected["result"] == "failed" and launch_rejected["ok"] is False

    second = WindowInfo(hwnd=2, pid=43, process_name="excel", window_class="FakeWnd",
                        title="Budget - Excel", bounds=(0, 0, 1280, 720))
    ambiguous_world = FakeComputerBackend()
    ambiguous_world.set_windows([book, second])
    ambiguous_world.app_windows = [
        AppWindowCandidate(window=book, doc_token="book1", unsaved_candidate=True),
        AppWindowCandidate(window=second, doc_token="budget", unsaved_candidate=False),
    ]
    ambiguous = await _drive(ambiguous_world, "excel|nomatch")
    validate_reply(ambiguous, "flat_execute")
    assert ambiguous["ensure_app_outcome"] == "ambiguous"
    assert ambiguous["result"] == "uncertain" and ambiguous["ok"] is False


# --- DELTA 3 (VAL-WP1-02 N1..N7) ---------------------------------------------------------------------


async def test_queue_entry_field_set_matches_flat_for_non_rejection_kinds(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N1: reasons exist ONLY on rejection kinds - executed and approval entries carry
    NO reasons key, exactly like the flat reply of the same kind."""
    # executed entries: no reasons key
    backend = ScriptedBackend(flip=True)
    session_id, _bundle, backend, _ = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    payload = execute_payload(
        await server.computer_execute(
            session_id, "click", x=10, y=10, approved=True, include_screenshot_after=False,
            follow_ups=[{"action": "done"}],
        )
    )
    for entry in payload["follow_up_results"]:
        assert "reasons" not in entry, entry

    # approval entry (session requires approval, call unapproved): no reasons key
    session_id2, _b2, _bk2, _p2 = make_session(
        monkeypatch, dry_run=False, require_approval=True, limits=FAST_LIMITS
    )
    payload2 = await server.computer_execute(
        session_id2, "click", x=10, y=10, follow_ups=[{"action": "click", "x": 30, "y": 30}]
    )
    assert payload2["result"] == "not_executed"
    assert "reasons" not in payload2  # flat: no reasons key
    for entry in payload2.get("follow_up_results") or []:
        assert entry["result"] == "not_executed"
        assert "reasons" not in entry, entry  # entry: identical field set as flat


def test_half_res_one_by_one_source_reports_pass_through_none() -> None:
    """N3: the 1x1/step-0 path - the half-res step cannot shrink a 1x1 frame, so the
    measured scale is the pass-through None (never a TypeError from None math)."""
    import base64 as _b64
    import io as _io

    from PIL import Image as _Image

    from computer_use_mcp import server

    image = _Image.new("RGB", (1, 1), "white")
    buffer = _io.BytesIO()
    image.save(buffer, format="PNG")
    png = _b64.b64encode(buffer.getvalue()).decode("ascii")
    result = server._half_res_action_image(png)
    assert result is not None
    _data, mime, scale = result
    assert mime == "image/jpeg"
    assert scale is None  # 1x1 -> half-res is a no-op: no downscale applied
    # and the scalar helper agrees for the identity case
    assert server._outbound_image_scale(1, 1, 1, 1) is None


def test_sendinput_counts_code_points_not_utf16_units(real_backend: Any) -> None:
    """N4: the SendInput engine's intra-call count is in CODE POINTS - an astral
    emoji travels as a surrogate PAIR (4 events) but counts ONE (engine stubbed via
    ``_send``; no real input)."""
    if platform.system() != "Windows":
        pytest.skip("Real Win32 backend requires Windows.")
    from computer_use_mcp.backend import SendInputEngine

    probes = {"n": 0}

    def focus_hook() -> None:
        probes["n"] += 1
        if probes["n"] == 7:  # drift before the 7th per-char dispatch (paced path)
            raise FocusDriftError("FOCUS_DRIFTED")

    engine = SendInputEngine(type_interval=0.01)
    engine._send = lambda events: None  # type: ignore[method-assign] - no real input
    text = "😀" * 10  # ten astral emoji
    with pytest.raises(FocusDriftError) as excinfo:
        engine.type_text(text, before_chunk=focus_hook)
    # paced path: one hook per 2 EVENTS = per surrogate HALF, so an emoji completes
    # every 2 hooks. Drift at probe 7 -> 6 hooks returned = 3 full emoji = exactly
    # 3 CODE POINTS (the old UTF-16-unit counting would have reported 6).
    assert excinfo.value.delivered_chars == 3, excinfo.value.delivered_chars


def test_sendinput_paced_per_char_drift_counts_dispatched(real_backend: Any) -> None:
    """N7: SendInput with type_interval>0 paces ONE character per hook call - a drift
    at hook N leaves exactly N-1 characters dispatched (engine stubbed, no real
    input)."""
    if platform.system() != "Windows":
        pytest.skip("Real Win32 backend requires Windows.")
    from computer_use_mcp.backend import SendInputEngine
    from computer_use_mcp.models import GroundedAction

    probes = {"n": 0}

    def focus_hook() -> None:
        probes["n"] += 1
        if probes["n"] == 8:
            raise FocusDriftError("FOCUS_DRIFTED")

    engine = SendInputEngine(type_interval=0.01)
    engine._send = lambda events: None  # type: ignore[method-assign] - no real input
    backend_patch = pytest.MonkeyPatch()
    try:
        backend = real_backend
        backend_patch.setattr(backend, "_engine", engine)
        backend_patch.setattr(backend, "_apply_focus_settle", lambda: None)
        action = GroundedAction(action="type", text="D" * 30, confidence=1.0)
        with pytest.raises(FocusDriftError) as excinfo:
            backend._execute_type(action, None, focus_hook)
        assert excinfo.value.delivered_chars == 7, excinfo.value.delivered_chars
    finally:
        backend_patch.undo()


async def test_ensure_app_failed_outcome_stops_the_queue(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N6: an ensure_app item whose typed outcome is a failure/ambiguous (ok False,
    no target attached) stops the batch - later items never run against a target that
    was never established."""
    import json

    # launch="driver" policy: the ensure_app probe reports a bare NO_INSTANCE
    # (nothing matched, nothing launched) -> typed not_found
    session_id, bundle, backend, _ = make_session(
        monkeypatch, backend=FakeComputerBackend(), dry_run=False, require_approval=False,
        limits=FAST_LIMITS,
        interference={"attach_or_launch": {"launch": "driver"}},
    )
    payload = execute_payload(
        await server.computer_execute(
            session_id, "click", x=10, y=10, approved=True, include_screenshot_after=False,
            follow_ups=[{"action": "ensure_app", "target": "excel|book1"},
                        {"action": "click", "x": 30, "y": 30}],
        )
    )
    entries = payload["follow_up_results"]
    assert len(entries) == 2, entries  # the third item never ran
    assert entries[1]["ensure_app_outcome"] == "not_found"
    assert entries[1]["result"] == "failed" and entries[1]["ok"] is False
    assert payload["follow_ups_stopped_reason"] == "ensure_app_failed"
    validate_reply(payload, "flat_execute")
    for entry in entries:
        validate_reply(entry, "follow_up_item")
    assert len(backend.executed) == 1  # only item 0 dispatched input
    # the queue audit records the named stop
    events = [json.loads(line) for line in bundle.auditor.path_for(session_id)
              .read_text(encoding="utf-8").splitlines() if line.strip()]
    queue_events = [e for e in events if e["event_type"] == "queue"]
    assert queue_events and queue_events[-1]["metadata"]["stopped_reason"] == "ensure_app_failed"


async def test_mid_batch_limit_does_not_count_the_tripped_item_as_executed(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N6: the limit-tripped item dispatched NOTHING and is not counted as executed
    in the queue audit metadata."""
    import json

    session_id, bundle, _backend, _ = make_session(
        monkeypatch, dry_run=False, require_approval=False,
        limits={"min_screenshot_interval_ms": 0, "max_actions": 1},
    )
    await server.computer_execute(
        session_id, "click", x=10, y=10, approved=True,
        follow_ups=[{"action": "click", "x": 30, "y": 30}],
    )
    events = [json.loads(line) for line in bundle.auditor.path_for(session_id)
              .read_text(encoding="utf-8").splitlines() if line.strip()]
    queue_events = [e for e in events if e["event_type"] == "queue"]
    assert queue_events and queue_events[-1]["metadata"]["stopped_reason"] == "limit_exceeded"
    assert queue_events[-1]["metadata"]["executed"] == 1  # item 1 dispatched nothing




# --- WP2: the input-engine duration fields on the contract surfaces --------------------------


async def test_held_click_follow_up_item_reply_verified(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WP2 §-node: a queued HELD click's reply obeys the §1 contract on the
    follow_up_item surface (pixel-flip fake -> verified), and the echo names the
    hold exactly. The primary flat reply is validated too."""
    backend = ScriptedBackend(flip=True)
    session_id, _bundle, backend, _ = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    payload = execute_payload(
        await server.computer_execute(
            session_id, "click", x=10, y=10, approved=True, include_screenshot_after=False,
            follow_ups=[{"action": "click", "x": 30, "y": 30, "hold_ms": 250}],
        )
    )
    validate_reply(payload, "flat_execute")
    entries = payload["follow_up_results"]
    assert len(entries) == 2  # index 0 = the primary, index 1 = the queued held click
    for entry in entries:
        validate_reply(entry, "follow_up_item")
    assert entries[1]["result"] == "verified"
    assert entries[1]["ok"] is True
    assert entries[1]["hold_ms"] == 250
    assert backend.executed[1].hold_ms == 250  # the dispatched twin


async def test_held_click_follow_up_item_reply_uncertain(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WP2 §-node: the same held click against the static-screen fake -> the honest
    ``uncertain`` token (never upgraded), still contract-valid."""
    backend = ScriptedBackend(flip=False)
    session_id, _bundle, backend, _ = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    payload = execute_payload(
        await server.computer_execute(
            session_id, "click", x=10, y=10, approved=True, include_screenshot_after=False,
            follow_ups=[{"action": "click", "x": 30, "y": 30, "hold_ms": 100}],
        )
    )
    entries = payload["follow_up_results"]
    for entry in entries:
        validate_reply(entry, "follow_up_item")
    assert entries[1]["result"] == "uncertain"
    assert entries[1]["ok"] is False
    assert entries[1]["hold_ms"] == 100


async def test_hold_ms_misuse_is_a_rejection_envelope(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WP2 §-node: ``hold_ms`` on a non-hold action is a §1 rejection —
    {ok False, result 'rejected', reasons[...]}, the typed ``invalid_action`` code
    first, the enforcing rule named in the message, NEVER an ``error`` key.
    Zero items run (the boundary pre-validates every queue item)."""
    backend = ScriptedBackend(flip=True)
    session_id, _bundle, backend, _ = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    payload = execute_payload(
        await server.computer_execute(
            session_id, "click", x=10, y=10, approved=True, include_screenshot_after=False,
            follow_ups=[{"action": "move", "x": 30, "y": 30, "hold_ms": 250}],
        )
    )
    validate_reply(payload, "flat_execute")
    assert payload["result"] == "rejected"
    assert_rejection(payload)
    assert payload["reasons"][0] == "invalid_action"
    assert "hold_ms is only valid on click/right_click/keypress/hotkey actions" in payload["message"]
    assert backend.executed == []  # nothing dispatched


async def test_duration_ms_misuse_is_a_rejection_envelope(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WP2 §-node: ``duration_ms`` on a non-move action is the same §1 rejection."""
    backend = ScriptedBackend(flip=True)
    session_id, _bundle, backend, _ = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    payload = execute_payload(
        await server.computer_execute(
            session_id, "click", x=10, y=10, approved=True, include_screenshot_after=False,
            follow_ups=[{"action": "click", "x": 30, "y": 30, "duration_ms": 200}],
        )
    )
    validate_reply(payload, "flat_execute")
    assert payload["result"] == "rejected"
    assert_rejection(payload)
    assert payload["reasons"][0] == "invalid_action"
    assert "duration_ms is only valid on move actions" in payload["message"]
    assert backend.executed == []


async def test_queued_hold_ms_out_of_band_is_a_rejection_envelope(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WP2 §-node: an out-of-band ``hold_ms`` (over MAX_HOLD_MS) is rejected at the
    ActionSpec schema itself — the same §1 rejection envelope."""
    backend = ScriptedBackend(flip=True)
    session_id, _bundle, backend, _ = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    payload = execute_payload(
        await server.computer_execute(
            session_id, "click", x=10, y=10, approved=True, include_screenshot_after=False,
            follow_ups=[{"action": "keypress", "keys": ["a"], "hold_ms": 9999}],
        )
    )
    validate_reply(payload, "flat_execute")
    assert payload["result"] == "rejected"
    assert_rejection(payload)
    assert backend.executed == []


async def test_invalid_follow_up_entry_field_set_parity(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """VAL-WP1 carry-over: the hand-built pre-dispatch ``invalid_follow_up`` queue
    entry carries the SAME field set as every pipeline-built entry — ``action_id``
    and ``ensure_app_outcome`` present (None: nothing dispatched, nothing to own a
    value). Pinned on the agent-level outcome (the boundary answers the same wire
    condition with the teaching rejection before the queue ever runs)."""
    from computer_use_mcp.agent import ComputerUseAgent
    from computer_use_mcp.models import ActionSpec, ActionType, GroundedAction

    backend = ScriptedBackend(flip=False)
    _session_id, bundle, backend, _ = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    agent: ComputerUseAgent = bundle.agent
    primary = GroundedAction(
        action=ActionType.CLICK, point={"x": 10, "y": 10}, reason="parity pin", confidence=1.0
    )
    outcome = await agent.run_single(
        bundle.state,
        primary,
        approved=True,
        follow_ups=[ActionSpec(action=ActionType.FOCUS_WINDOW)],  # no target: invalid
    )
    assert outcome.kind == "rejected"
    assert outcome.follow_ups_stopped_reason == "invalid_follow_up"
    entry = (outcome.follow_up_results or [])[0]
    assert entry["action_id"] is None
    assert entry["ensure_app_outcome"] is None
    assert entry["result"] == "rejected" and entry["ok"] is False
    # DELTA 1 (F8): the hand-built entry must satisfy the §1 follow_up_item contract
    # on its own (rejection envelope, no error key), and its KEY SET must equal a
    # PIPELINE-BUILT rejected entry's key set exactly — parity is not established by
    # subtracting keys, only by comparing the real shapes.
    validate_reply(entry, "follow_up_item")

    rejected_outcome = await agent.run_single(
        bundle.state,
        GroundedAction(
            action=ActionType.CLICK, point={"x": 14, "y": 14}, reason="parity twin", confidence=1.0
        ),
        approved=True,
        # a pipeline-built REJECTION: the item passes conversion but fails grounding
        # validation in the pipeline (missing keys), so it gets the full §1 treatment
        follow_ups=[ActionSpec(action=ActionType.KEYPRESS, keys=[])],
    )
    built = (rejected_outcome.follow_up_results or [])[1]
    assert built["kind"] == "rejected" and built["result"] == "rejected"
    validate_reply(built, "follow_up_item")
    assert set(entry) == set(built), (sorted(entry), sorted(built))


# --- WP3 (ORVEX-2026-105): identity-change verification + interference events -----------------


def _dialog_backend() -> ScriptedBackend:
    """A static-screen session (pixels never change) whose click opens a 'Confirm
    Delete?' dialog — the canonical window-identity-change scenario."""
    backend = ScriptedBackend(
        flip=False,  # pixels never change — only the window identity moves
        active_window=WindowInfo(hwnd=1, pid=10, process_name="app.exe", title="Main"),
    )
    dialog = WindowInfo(hwnd=2, pid=99, process_name="app.exe", title="Confirm Delete?")
    backend.execute_hooks.append(lambda action: backend.set_active_window(dialog))
    return backend


async def test_identity_change_with_the_named_identity_evidenced_verifies(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WP3 §-node: a click whose stated effect names the thing that ACTUALLY appeared
    (the after window identity carries it, novel against the before identity) verifies
    honestly through the deterministic identity tier — ok True via ``verified``."""
    backend = _dialog_backend()
    session_id, _bundle, _backend, _ = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    payload = execute_payload(
        await server.computer_execute(
            session_id, "click", x=50, y=50, expected_effect="The Confirm dialog appears"
        )
    )
    validate_reply(payload, "flat_execute")
    assert payload["result"] == "verified"
    assert payload["ok"] is True
    assert payload["verification"]["outcome"] == "verified"
    assert payload["verification"]["verification_method"] == "focus_change"
    assert any(
        "corroborated by effect token" in str(line)
        for line in payload["verification"]["evidence"]
    )


async def test_identity_change_without_the_named_identity_is_an_honest_uncertain(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WP3 §-node: the SAME identity change, but the stated effect names something the
    after identity does not ('the record is deleted' vs a confirm prompt) — the reply
    demotes to the honest ``uncertain`` token (ok False, never a silent ok:True), and
    the verification block carries the demoted outcome."""
    backend = _dialog_backend()
    session_id, bundle, backend, _ = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    payload = execute_payload(
        await server.computer_execute(
            session_id, "click", x=50, y=50, expected_effect="the record is deleted"
        )
    )
    validate_reply(payload, "flat_execute")
    assert payload["result"] == "uncertain"
    assert payload["ok"] is False  # a verification demotion is NEVER ok:true
    assert payload["verification"]["outcome"] == "uncertain"
    # the click itself still dispatched and ran exactly as before
    assert executed_summary(backend) == [("click", (50, 50), None)]
    # F8 (VAL-WP4a DELTA 2): the stated-effect static-screen click takes the ONE
    # bounded late-repaint re-capture, and the single-record structure still yields
    # exactly ONE verification latency sample for the whole (recaptured) verification.
    assert bundle.metrics.snapshot()["latencies"]["verification_ms"]["count"] == 1


async def test_rejection_reply_carries_the_retained_interference_events(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WP3 §-node: a pre-dispatch guard refusal (foreign foreground steal) surfaces the
    retained event list on the reply — the same string as the first reason — and the
    rejection obeys the §1 envelope (reasons, never an ``error`` key)."""
    from computer_use_mcp.interference import FOCUS_TAKEN_BY

    backend = ScriptedBackend(
        flip=True, active_window=WindowInfo(hwnd=1, pid=100, process_name="EXCEL.EXE",
                                            window_class="XLMAIN", title="Book1 - Excel")
    )
    session_id, bundle, backend, _ = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    bundle.agent.guard.rebind(
        WindowInfo(hwnd=1, pid=100, process_name="EXCEL.EXE", window_class="XLMAIN",
                   title="Book1 - Excel")
    )
    backend.set_active_window(
        WindowInfo(hwnd=9, pid=900, process_name="notepad.exe", window_class="Notepad",
                   title="user notes - Notepad")
    )
    payload = await server.computer_execute(session_id, "click", x=10, y=10)
    validate_reply(payload, "flat_execute")
    assert_rejection(payload)
    event = payload["reasons"][0]
    assert event.startswith(FOCUS_TAKEN_BY), payload
    assert payload["interference_events"] == [event], payload
    assert backend.executed == []  # nothing dispatched on the refusal


async def test_verified_follow_up_entry_evidence_names_the_matched_token(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WP3 §-node (F5, VAL-WP3): a VERIFIED queued follow-up's entry evidence still
    names its matched token — the R-9 self-naming corroboration line rides FIRST in the
    focus_change evidence, so it survives the entry's bounded evidence window (the
    first three lines)."""
    backend = ScriptedBackend(
        flip=False,
        active_window=WindowInfo(hwnd=1, pid=10, process_name="app.exe", title="Main"),
    )
    session_id, _bundle, backend, _ = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False,
        limits=FAST_LIMITS,
    )
    # the dialog opens on the SECOND dispatch only: item 0 (the primary click, no
    # stated effect) runs against "Main"; item 1's dispatch is the identity change its
    # effect names
    dialog = WindowInfo(hwnd=2, pid=99, process_name="app.exe", title="Confirm Delete?")
    dispatches = {"n": 0}

    def open_dialog_on_second_dispatch(action: Any) -> None:
        dispatches["n"] += 1
        if dispatches["n"] == 2:
            backend.set_active_window(dialog)

    backend.execute_hooks.append(open_dialog_on_second_dispatch)
    payload = execute_payload(
        await server.computer_execute(
            session_id, "click", x=50, y=50, approved=True, include_screenshot_after=False,
            follow_ups=[
                {"action": "click", "x": 50, "y": 50,
                 "expected_effect": "The Confirm dialog appears"},
            ],
        )
    )
    validate_reply(payload, "flat_execute")
    entries = payload["follow_up_results"]
    assert len(entries) == 2, entries  # index 0 = the done marker, 1 = the queued click
    for entry in entries:
        validate_reply(entry, "follow_up_item")
    assert entries[1]["result"] == "verified" and entries[1]["ok"] is True
    evidence = entries[1]["verification"]["evidence"]
    assert evidence, entries[1]
    assert evidence[0].startswith("corroborated by effect token"), evidence
    assert "'confirm'" in evidence[0]  # the matched token, named inside the cap


# --- WP4a (ORVEX-2026-108): verification accuracy on the reply surface ----------------------------


async def test_identity_change_plus_strong_pixel_change_with_unrelated_effect_is_uncertain(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WP4a §-node (the reply-level shape of the flipped WP3 xfail): a click that BOTH
    changes the window identity AND strongly repaints the screen (flip fake: white ->
    black, far above the corroboration floor) never verifies an unrelated stated
    effect — ok False / result ``uncertain`` — and the audit 'verification' event
    agrees with the reply verdict."""
    backend = ScriptedBackend(
        flip=True,  # the dispatch strongly repaints the screen
        active_window=WindowInfo(hwnd=1, pid=10, process_name="app.exe", title="Main"),
    )
    dialog = WindowInfo(hwnd=2, pid=99, process_name="app.exe", title="Confirm Delete?")
    backend.execute_hooks.append(lambda action: backend.set_active_window(dialog))
    session_id, bundle, backend, _ = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    payload = execute_payload(
        await server.computer_execute(
            session_id, "click", x=50, y=50, expected_effect="the record is deleted"
        )
    )
    validate_reply(payload, "flat_execute")
    assert payload["result"] == "uncertain"
    assert payload["ok"] is False  # a contradicted/uncorroborated effect is NEVER ok:true
    assert payload["verification"]["outcome"] == "uncertain"
    assert payload["verification"]["verified"] is False
    assert executed_summary(backend) == [("click", (50, 50), None)]  # the click ran once
    verification_events = [
        e for e in audit_events(bundle, session_id) if e["event_type"] == "verification"
    ]
    # F8 (VAL-WP4a DELTA 2): exactly ONE verification audit event...
    assert len(verification_events) == 1, verification_events
    assert verification_events[-1]["result"] == "uncertain", verification_events[-1]


class _MaskedPinWorld(ScriptedBackend):
    """A login dialog whose focused-control read-back models the R-04 reader (the
    tests/test_a3_mismatch_blocks_verified.py world, on the server surface)."""

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


async def test_type_integrity_mismatch_vetoes_verified_and_audit_agrees(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WP4a §-node (F2-1): a masked PIN field (read-back ``integrity=mismatch``) plus a
    window title that mirrors the typed text never answers ``ok:true`` — the
    strong, target-scoped read-back vetoes the weak substring match, the reply is the
    honest ``uncertain`` token, and the audit 'verification' event (written AFTER the
    veto) carries the SAME verdict."""
    backend = _MaskedPinWorld(title="Sign in - account 1234", focused_control_value="••••")
    session_id, bundle, backend, _ = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    payload = execute_payload(
        await server.computer_execute(session_id, "type", text="1234", approved=True)
    )
    validate_reply(payload, "flat_execute")
    assert "integrity=mismatch" in payload["message"], payload["message"]
    assert payload["result"] == "uncertain"
    assert payload["ok"] is False  # a contradicted expected effect is NEVER ok:true
    assert payload["verification"]["outcome"] == "uncertain"
    assert payload["verification"]["verified"] is False
    evidence = " | ".join(payload["verification"]["evidence"])
    assert "mismatch" in evidence, payload["verification"]["evidence"]
    verification_events = [
        e for e in audit_events(bundle, session_id) if e["event_type"] == "verification"
    ]
    # F8 (VAL-WP4a DELTA 2): exactly ONE verification audit event...
    assert len(verification_events) == 1, verification_events
    assert verification_events[-1]["result"] == "uncertain", verification_events[-1]
    assert executed_summary(backend) == [("type", None, "1234")]  # typed exactly once


async def test_verification_audit_names_the_real_ladder_tier_on_the_plain_path(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F1 (VAL-WP4a DELTA 2): the PRIMARY ladder run records its REAL tier in the
    verification audit metadata — on this plain path (no late-repaint recapture: no
    stated effect; no expected-text fallback: the intent is visual_change) the tier
    is the engine facade, and the sample count is exactly one."""
    backend = ScriptedBackend(flip=True)  # dispatch flips pixels -> provable change
    session_id, bundle, backend, _ = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    payload = execute_payload(
        await server.computer_execute(session_id, "click", x=10, y=10)
    )
    validate_reply(payload, "flat_execute")
    assert payload["result"] == "verified"
    assert payload["ok"] is True
    events = [
        e for e in audit_events(bundle, session_id) if e["event_type"] == "verification"
    ]
    assert len(events) == 1, events
    assert events[0]["result"] == "verified", events[0]
    assert events[0]["metadata"]["ladder_tier"] == "engine", events[0]
    assert bundle.metrics.snapshot()["latencies"]["verification_ms"]["count"] == 1
