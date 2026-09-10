"""D1 image delivery tests (ORVEX-CORTEX-056-LIVEFIX, defect D1).

Pins the F-1 design (.orvex/artifacts/f1-image-delivery-design.md): a NON-VISION
model receiving ONE ImageContent block anywhere in its session history gets the
ENTIRE provider request rejected with a 400 ('content' must be a string) — the
turn dies and every later turn replays the poisoned image. The delivery mode is
decided at start_session (param > env ``CORTEX_IMAGE_DELIVERY`` > default
"image"), stored in bundle.extra, and applied at every outbound emission point:

  observe    -> text mode returns ONE TextContent (metadata + mode keys), never
                an ImageContent; image mode stays byte-identical [text, image];
  execute    -> text mode returns the slim dict (screenshot popped, mode keys
                added); include_screenshot_after can never re-enable images;
  screenshot -> inherits via the observe delegation.

AMENDMENT (2026-09-08, R-2 ruling, APPLIED): the run_goal family (run_goal, run_subtask,
create_subtask, list_subtasks, get_session_progress) is REMOVED PERMANENTLY from
0.5.5 — the text-mode loop pins died with the loop; the direct-path pins below are
the surviving contract.

Internal capture, digests, metrics, audit, and every security gate are UNTOUCHED
in both modes (outbound-only doctrine). Invalid param values fail CLOSED with
``invalid_image_delivery`` before any session exists (a typo silently meaning
"image" would re-arm the killer).
"""

from __future__ import annotations

import inspect
import json
from typing import Any

import pytest
from test_controller_integration import (
    FAST_LIMITS,
    ScriptedBackend,
    make_session,
)

from computer_use_mcp import server
from computer_use_mcp.state import SessionRegistry


@pytest.fixture
def fresh_server(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Any:
    """Fresh bounded registry/bundles + per-test audit dir + env knob cleared."""
    monkeypatch.setenv("COMPUTER_USE_MCP_LOG_DIR", str(tmp_path / "audit"))
    monkeypatch.delenv(server.IMAGE_DELIVERY_ENV, raising=False)
    monkeypatch.setattr(server, "_registry", SessionRegistry(max_sessions=8))
    monkeypatch.setattr(server, "_bundles", {})
    return server


# --- real-boundary harness (same surface the stdio transport uses) ------------------------------


def _meta(tool_name: str) -> Any:
    tool = server.mcp._tool_manager.get_tool(tool_name)
    assert tool is not None, f"tool {tool_name} not registered"
    return tool.fn_metadata


async def _call_tool(tool_name: str, fn: Any, arguments: dict[str, Any]) -> Any:
    meta = _meta(tool_name)
    pre = meta.pre_parse_json(dict(arguments))
    model = meta.arg_model.model_validate(pre)
    kwargs = model.model_dump_one_level()
    result = fn(**kwargs)
    if inspect.isawaitable(result):
        result = await result
    return result


async def boundary_start(**arguments: Any) -> Any:
    return await _call_tool("start_session", server.start_session, dict(arguments))


async def boundary_execute(session_id: str, **arguments: Any) -> Any:
    arguments.setdefault("action", "click")
    return await _call_tool(
        "computer_execute", server.computer_execute, {"session_id": session_id, **arguments}
    )


class _FakeProvider:
    """Minimal lazy-provider stand-in (never constructs the real vision provider)."""

    async def decide(self, goal: str, observation: Any, history: list[str]) -> Any:
        raise RuntimeError("fake provider: no decide in this test")

    async def decide_full(self, goal: str, observation: Any, history: list[str]) -> Any:
        raise RuntimeError("fake provider: no decide_full in this test")


def _make_text_session(
    monkeypatch: pytest.MonkeyPatch, **start_kwargs: Any
) -> tuple[str, Any, Any]:
    """A live text-mode session through the REAL boundary (approval off).

    Both factories are ALWAYS faked: constructing the real LocalComputerBackend
    here would consume the process's one-time DPI declaration and degrade the
    shared session-scoped ``real_backend`` fixture (conftest.py).
    """
    backend = ScriptedBackend(width=1920, height=1080)
    monkeypatch.setattr(server, "_backend_factory", lambda: backend)
    monkeypatch.setattr(server, "_provider_factory", lambda: _FakeProvider())
    start_kwargs.setdefault("dry_run", False)
    start_kwargs.setdefault("require_approval", False)
    start_kwargs.setdefault("limits", FAST_LIMITS)
    start_kwargs.setdefault("image_delivery", "text")
    response = server.start_session(**start_kwargs)
    assert response.get("session_id"), response
    session_id = str(response["session_id"])
    bundle = server._get_bundle(session_id)
    return session_id, bundle, backend


def _all_base64_images(node: Any) -> list[str]:
    """Collect every screenshot/image blob in a response tree (recursive)."""
    found: list[str] = []

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"screenshot_after_base64", "image_base64"} and isinstance(item, str):
                    found.append(item)
                elif key == "data" and isinstance(item, str) and len(item) > 1000:
                    found.append(item)
                else:
                    visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(node)
    return found


# --- pin 1: text-mode observe emits zero image blocks --------------------------------------------


async def test_text_mode_observe_emits_zero_image_blocks(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """start_session(image_delivery="text") -> observe returns exactly ONE text
    block; no image block; metadata carries the mode keys + text_summary + digest."""
    session_id, _bundle, _backend = _make_text_session(monkeypatch)
    result = server.computer_observe(session_id)
    assert isinstance(result, list) and len(result) == 1, result
    block = result[0]
    assert block.type == "text"
    metadata = json.loads(block.text)
    assert metadata["image_delivery"] == "text"
    assert metadata["image_delivery_note"] == server.IMAGE_DELIVERY_TEXT_NOTE
    assert metadata["image_format"] == "none"
    assert metadata["text_summary"]
    assert metadata["digest"]
    assert "observation" in metadata


# --- pin 2: internal capture intact in text mode ------------------------------------------------


async def test_text_mode_observe_internal_capture_intact(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Text mode suppresses the OUTBOUND block only: digest updates, screenshot
    metrics increment, and the internal Observation still carries real PNG bytes."""
    session_id, bundle, _backend = _make_text_session(monkeypatch)
    before_counters = bundle.metrics.snapshot()["counters"].get("screenshot_count", 0)
    result = server.computer_observe(session_id)
    assert isinstance(result, list) and len(result) == 1
    assert bundle.extra["last_observation_digest"]
    assert (
        bundle.metrics.snapshot()["counters"]["screenshot_count"] == before_counters + 1
    )
    observation = bundle.agent.observation.capture()
    assert observation.image_base64  # internal capture keeps real bytes
    import base64

    assert base64.b64decode(observation.image_base64)[:8] == b"\x89PNG\r\n\x1a\n"


# --- pin 3: image-mode default unchanged --------------------------------------------------------


async def test_image_mode_default_unchanged(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No env, no param -> observe returns [TextContent, ImageContent] (REM-A/E
    parity intact: the D1 gate must never apply in image mode)."""
    backend = ScriptedBackend()
    monkeypatch.setattr(server, "_backend_factory", lambda: backend)
    monkeypatch.setattr(
        server, "_provider_factory", lambda: _FakeProvider()
    )  # never construct the real vision provider either
    response = server.start_session(
        dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    session_id = str(response["session_id"])
    assert response["image_delivery"] == "image"
    assert "image_delivery_note" not in response
    result = server.computer_observe(session_id)
    assert isinstance(result, list) and len(result) == 2, result
    assert result[0].type == "text"
    assert result[1].type == "image"


# --- pin 4: precedence param > env --------------------------------------------------------------


async def test_precedence_param_over_env(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """param "text" beats env "image"; param "image" beats env "text"."""
    monkeypatch.setenv(server.IMAGE_DELIVERY_ENV, "image")
    session_id, _bundle, _b = _make_text_session(monkeypatch)  # param text
    assert server._get_bundle(session_id).extra["image_delivery"] == "text"

    monkeypatch.setenv(server.IMAGE_DELIVERY_ENV, "text")
    backend = ScriptedBackend()
    monkeypatch.setattr(server, "_backend_factory", lambda: backend)
    monkeypatch.setattr(server, "_provider_factory", lambda: _FakeProvider())
    response = server.start_session(
        dry_run=False,
        require_approval=False,
        limits=FAST_LIMITS,
        image_delivery="image",
    )
    assert response["image_delivery"] == "image"
    result = server.computer_observe(str(response["session_id"]))
    assert isinstance(result, list) and len(result) == 2  # real image block


# --- pin 5: env layer fail-safe -----------------------------------------------------------------


async def test_env_layer(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """env "text" alone -> text; "TEXT"/" text " -> text (trim/case); garbage or
    empty -> image (fail-safe: a typo never arms the killer, never blinds vision).

    Every start goes through a FAKE backend factory — a real LocalComputerBackend
    constructed here would degrade the shared session-scoped ``real_backend``
    fixture's DPI state (one DPI declaration per process; conftest.py).
    """
    backend = ScriptedBackend()

    def _start() -> str:
        monkeypatch.setattr(server, "_backend_factory", lambda: backend)
        monkeypatch.setattr(server, "_provider_factory", lambda: _FakeProvider())
        response = server.start_session(
            dry_run=False, require_approval=False, limits=FAST_LIMITS
        )
        return str(response["session_id"])

    for raw, expected in (
        ("text", "text"),
        ("TEXT", "text"),
        (" text ", "text"),
        ("banana", "image"),
        ("", "image"),
    ):
        monkeypatch.setenv(server.IMAGE_DELIVERY_ENV, raw)
        session_id = _start()
        assert server._get_bundle(session_id).extra["image_delivery"] == expected, raw
        server.stop_session(session_id)
    monkeypatch.delenv(server.IMAGE_DELIVERY_ENV, raising=False)
    assert server._resolve_image_delivery(None) == "image"  # unset -> default


# --- pin 6: invalid param fails closed pre-registry ---------------------------------------------


async def test_invalid_param_fail_closed(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """param "banana" -> {"ok": False, "error": "invalid_image_delivery"} AND no
    session registered (mirrors the invalid_limits guard — no registry slot leaks)."""
    monkeypatch.setattr(server, "_backend_factory", lambda: ScriptedBackend())
    monkeypatch.setattr(server, "_provider_factory", lambda: _FakeProvider())
    assert server._bundles == {}
    response = await boundary_start(image_delivery="banana")
    assert response["ok"] is False
    assert response["error"] == "invalid_image_delivery"
    assert "image" in response["message"].lower()
    assert "text" in response["message"].lower()  # teaching error names both values
    assert server._bundles == {}  # no bundle leaked
    # A follow-up valid call still starts cleanly (the registry slot was never taken).
    good = await boundary_start(allowed_processes=["mspaint.exe"])
    assert good.get("session_id"), good
    assert good["image_delivery"] == "image"


# --- pin 7: text-mode executed execute returns the slim dict ------------------------------------


async def test_text_mode_executed_execute_returns_slim_dict(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An executed click in text mode returns a PLAIN DICT (no block list), no
    screenshot bytes, mode keys present; a rejection shape stays mode-independent."""
    session_id, _bundle, backend = _make_text_session(monkeypatch)
    result = await server.computer_execute(session_id, "click", x=15, y=15)
    assert isinstance(result, dict), result
    assert result["ok"] is True
    assert "screenshot_after_base64" not in result
    assert result["image_delivery"] == "text"
    assert result["image_delivery_note"] == server.IMAGE_DELIVERY_TEXT_NOTE
    assert _all_base64_images(result) == []
    assert len(backend.executed) == 1  # the action still ran

    # Mode-independent shapes: a rejection stays a plain dict, unchanged by mode.
    rejected = await server.computer_execute(session_id, "key", keys=["a"])
    assert isinstance(rejected, dict) and rejected["ok"] is False
    assert "image_delivery" not in rejected or rejected.get("ok") is False


# --- pin 8: text mode dominates include_screenshot_after ---------------------------------------


async def test_text_mode_dominates_include_screenshot_after(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """include_screenshot_after=True (and None) can NEVER re-enable images in
    text mode — it can only remove bytes, never add them."""
    session_id, _bundle, _backend = _make_text_session(monkeypatch)
    for opt in (True, None):
        result = await server.computer_execute(
            session_id, "click", x=25, y=25, include_screenshot_after=opt
        )
        assert isinstance(result, dict), (opt, result)
        assert result["ok"] is True
        assert "screenshot_after_base64" not in result
        assert _all_base64_images(result) == []
        assert result["image_delivery"] == "text"


# --- pin 9: image-mode opt-out unchanged --------------------------------------------------------


async def test_image_mode_opt_out_unchanged(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Image mode + include_screenshot_after=False keeps the existing slim-dict
    shape (no mode keys injected — the opt-out contract is image-mode legacy)."""
    backend = ScriptedBackend()
    monkeypatch.setattr(server, "_backend_factory", lambda: backend)
    monkeypatch.setattr(server, "_provider_factory", lambda: _FakeProvider())
    response = server.start_session(
        dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    session_id = str(response["session_id"])
    result = await server.computer_execute(
        session_id, "click", x=25, y=25, include_screenshot_after=False
    )
    assert isinstance(result, dict) and result["ok"] is True
    assert "screenshot_after_base64" not in result
    assert _all_base64_images(result) == []


# --- pins 10/11 (run_goal / run_subtask text-mode loops): REMOVED BY AMENDMENT ------------------
# The Commander ruled (2026-09-08, mission record ORVEX-CORTEX-056-LIVEFIX) that the
# run_goal FAMILY (run_goal, run_subtask, create_subtask, list_subtasks,
# get_session_progress) is removed permanently from 0.5.5 by wave R-2, leaving the
# five deterministic tools. Per the R-1 contract amendment: do NOT build D1 text-
# mode gating into those paths and do NOT pin them here. The five-tool surface
# (start_session/stop_session/computer_observe/computer_screenshot/computer_execute)
# is fully gated and pinned above/below.


# --- pin 12: computer_screenshot alias inherits text mode ---------------------------------------


async def test_screenshot_alias_inherits_text_mode(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """computer_screenshot in text mode delegates to observe -> single text block."""
    session_id, _bundle, _backend = _make_text_session(monkeypatch)
    result = server.computer_screenshot(session_id)
    assert isinstance(result, list) and len(result) == 1, result
    assert result[0].type == "text"
    metadata = json.loads(result[0].text)
    assert metadata["image_delivery"] == "text"


# --- pin 13: param tolerance (REM-F) ------------------------------------------------------------


async def test_param_tolerance(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """"TEXT" and " text " normalize to "text" (trim + casefold coercion)."""
    monkeypatch.setattr(server, "_backend_factory", lambda: ScriptedBackend())
    monkeypatch.setattr(server, "_provider_factory", lambda: _FakeProvider())
    for raw in ("TEXT", " text "):
        response = await boundary_start(image_delivery=raw)
        assert response.get("session_id"), (raw, response)
        assert response["image_delivery"] == "text", raw
        server.stop_session(str(response["session_id"]))


# --- pin 14: resume is governed by the fresh param ----------------------------------------------


async def test_resume_governed_by_fresh_param(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """Session A text-mode checkpoints; resume_from_checkpoint with
    image_delivery="image" observes with a REAL image block (the mode is a
    fresh-session policy — deliberately never checkpointed).

    R-2 amendment note: the long-running runtime is armed through the module
    seam (``_build_runtime``) instead of a subtask TOOL — the run_goal family is
    removed by R-2 and this wave must not build on it.
    """
    from computer_use_mcp.checkpoint_manager import CheckpointManager
    from computer_use_mcp.resume_manager import ResumeManager

    checkpoint_manager = CheckpointManager(tmp_path / "checkpoints")
    monkeypatch.setattr(server, "_checkpoint_manager", checkpoint_manager)
    monkeypatch.setattr(server, "_resume_manager", ResumeManager(checkpoint_manager))

    backend = ScriptedBackend()
    monkeypatch.setattr(server, "_backend_factory", lambda: backend)
    monkeypatch.setattr(server, "_provider_factory", lambda: _FakeProvider())
    response = server.start_session(
        dry_run=False, require_approval=False, limits=FAST_LIMITS, image_delivery="text"
    )
    assert response["image_delivery"] == "text"
    session_id = str(response["session_id"])
    # The module seam (no run-goal-family TOOL call) arms the long-running runtime
    # so stop_session checkpoints; one real action gives it progress.
    bundle = server._get_bundle(session_id)
    bundle.extra["long_running"] = server._build_runtime(
        bundle, goal=str(bundle.context.task.goal or "")
    )
    await server.computer_execute(session_id, "click", x=15, y=15)
    server.stop_session(session_id)
    path = checkpoint_manager.checkpoint_path(session_id)
    assert path.exists(), "no checkpoint was written on stop"

    resumed = server.start_session(
        dry_run=False,
        require_approval=False,
        limits=FAST_LIMITS,
        image_delivery="image",
        resume_from_checkpoint=str(path),
    )
    assert resumed.get("session_id"), resumed
    assert resumed["image_delivery"] == "image"
    assert resumed.get("resumed") is True
    result = server.computer_observe(str(resumed["session_id"]))
    assert isinstance(result, list) and len(result) == 2, result
    assert result[1].type == "image"  # fresh param governs, not the checkpointed mode


# --- pin 15: REM-G extension — the advertised start_session schema ------------------------------
# (kept in test_rem_g_plain_schemas.py per the design work order; asserted here too
# so this suite is self-contained for D1.)


async def test_image_delivery_advertised_flat_nullable_string(
    fresh_server: Any,
) -> None:
    """start_session.image_delivery advertises {"type": ["string","null"],
    "default": null} — flat type-array, no anyOf (REM-G flattening holds)."""
    props = _meta("start_session").arg_model.model_json_schema()["properties"]
    flat = props["image_delivery"]
    assert flat.get("type") == ["string", "null"], flat
    assert flat.get("default") is None, flat
    assert "anyOf" not in flat, flat


# --- pin 16 (R-3, defect L1-NEW-1): the docstring teaching survives model
# self-misclassification — it targets the capability-JUDGMENT failure, not reading
# comprehension. The live probe (.orvex/live/l1_step3_untaught.log) showed the
# default model READ the old teaching, understood it, then self-misclassified
# ("my host is multimodal") and chose the default image mode anyway. The wording
# must therefore (a) REMOVE the decision from the model's self-assessment (judge
# by what it RECEIVES), (b) make the text-mode default suggestion explicit, and
# (c) state the honest asymmetry (text never crashes; image kills text-only
# sessions). The pinned substrings below are the load-bearing phrases..


async def test_start_session_docstring_teaches_judgment_by_receipt(
    fresh_server: Any,
) -> None:
    """The image_delivery teaching judges by INPUTS RECEIVED, not self-identity.

    Pins the L1-NEW-1 mitigation: the docstring must never ask the model whether
    it is "a vision model" (self-misclassification is the recorded failure mode);
    it must (a) teach judging by what the model receives, (b) mark "text" as
    REQUIRED for text-only inputs and the safe default when unsure, and (c) state
    the asymmetry honestly (text mode costs pixels for a vision model but never
    crashes; one image part in a text-only conversation kills the session).
    """
    doc = inspect.getdoc(server.start_session) or ""
    # collapse wrapping whitespace so the pins survive docstring re-flowing
    flat = " ".join(doc.split())
    assert "image_delivery" in doc, "the param must stay taught in the docstring"
    # (a) the decision keyed to received inputs, NOT to model self-identity:
    assert "JUDGE BY WHAT YOU RECEIVE" in flat
    # (b) text is REQUIRED for text-only inputs; "unsure" routes to text:
    assert 'image_delivery="text" — REQUIRED' in flat
    assert 'UNSURE? Pass "text"' in flat
    # (c) the honest asymmetry:
    assert "Text mode never crashes" in flat
    assert "costs the screenshot pixels" in flat
    # the killer consequence stays explicit (teaching must keep the WHY):
    assert "KILLS the whole session PERMANENTLY" in flat
    # observe's docstring cross-points to start_session (a model discovering
    # observe FIRST still learns where the mode is decided):
    observe_doc = inspect.getdoc(server.computer_observe) or ""
    assert "the delivery mode is fixed at start_session" in " ".join(observe_doc.split())


async def test_start_session_docstring_never_asks_model_to_self_classify(
    fresh_server: Any,
) -> None:
    """The teaching never hinges on "are you a vision model?" self-assessment.

    The old wording ("If you are NOT a vision model …") was read, understood, and
    IGNORED by a model that believed its HOST's multimodal branding (L1-NEW-1).
    The replacement keys the decision to the model's own inputs instead; this pin
    guards against a regression to identity-based wording.
    """
    doc = inspect.getdoc(server.start_session) or ""
    assert "If you are NOT a vision model" not in doc
