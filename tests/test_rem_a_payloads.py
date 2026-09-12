"""REM-A payload-parity + outbound-size tests (master-mission Phase 2, ORVEX-CORTEX-055).

H3/H5/H6/H7/H8/H1 regression suite:

- H3: ``computer_execute`` returns MCP content blocks in parity with
  ``computer_observe`` — one slim TextContent (result JSON WITHOUT the image blob)
  + one ImageContent carrying the post-action screenshot.
- H5: ``include_screenshot_after=False`` must hold on the follow_up_results path
  too — no base64 image bytes anywhere in the response (nested or otherwise).
- H6: per-item queue results are slim — no second copy of the top-level payload
  blobs (no ``screenshot_after_base64``, no duplicated action/verification objects).
- H7: the outbound image respects ``CORTEX_RESULT_IMAGE_MAX_KB`` — oversized PNGs
  are re-encoded as JPEG (progressive downscale fallback) in the OUTBOUND copy only;
  internal PNG bytes stay untouched.
- H8: observe metadata text bounds ``ocr_text``/``ui_elements`` (cap + omitted count)
  without touching the internal Observation model.
- H1: the provider judge path reuses the already-encoded base64 (no PIL re-encode).
"""

from __future__ import annotations

import base64
import io
import json
import os
from typing import Any

import pytest
from PIL import Image

from computer_use_mcp import server
from computer_use_mcp.models import Observation, TextRegion

from test_controller_integration import (
    FAST_LIMITS,
    ScriptedBackend,
    ScriptedProvider,
    _png,
    make_session,
)


@pytest.fixture
def fresh_server(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Any:
    """Fresh bounded registry/bundles + per-test audit dir (same as controller suite)."""
    monkeypatch.setenv("COMPUTER_USE_MCP_LOG_DIR", str(tmp_path / "audit"))
    monkeypatch.setattr(server, "_registry", __import__(
        "computer_use_mcp.state", fromlist=["SessionRegistry"]
    ).SessionRegistry(max_sessions=8))
    monkeypatch.setattr(server, "_bundles", {})
    return server


# --- helpers ---------------------------------------------------------------------------------


def _big_png(size: tuple[int, int] = (1600, 1200), color: str = "steelblue") -> str:
    """A PNG sized to exceed the default outbound image budget (>= 180 KB decoded)."""
    image = Image.new("RGB", size, color)
    # sprinkle non-uniform content so PNG is not trivially tiny and JPEG has texture
    for x in range(0, size[0], 40):
        for y in range(0, size[1], 40):
            image.putpixel((x, y), (200, 30, 40))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def _decoded_kb(data_b64: str) -> int:
    return len(base64.b64decode(data_b64, validate=True)) / 1024.0


def _all_base64_images(node: Any) -> list[str]:
    """Collect every ``data``/``screenshot_after_base64``/``image_base64`` blob in a tree."""
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


# --- H3: computer_execute returns MCP content blocks ------------------------------------------


async def test_computer_execute_returns_content_block_pair(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The executed shape is TextContent + ImageContent (parity with computer_observe)."""
    session_id, _bundle, backend, _ = make_session(
        monkeypatch, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    result = await server.computer_execute(session_id, "click", x=15, y=15)
    assert isinstance(result, list) and len(result) == 2, result
    text_block, image_block = result
    assert text_block.type == "text"
    assert image_block.type == "image"
    # RC-D10 (058): the DEFAULT executed-response image is a HALF-RESOLUTION JPEG
    # (0.5x, q60) so a driver's per-action context stops growing by 150-230KB PNG.
    assert image_block.mimeType == "image/jpeg"
    decoded = base64.b64decode(image_block.data, validate=True)
    assert decoded.startswith(bytes([0xFF, 0xD8]))  # real JPEG bytes (SOI)
    assert backend.executed  # the action really ran

    payload = json.loads(text_block.text)
    # Essential legacy fields all present in the text payload.
    for field in (
        "ok",
        "message",
        "action",
        "verification",
        "model_confidence",
        "grounding_confidence",
        "verification_confidence",
        "retry_count",
    ):
        assert field in payload, field
    # The heavy blob never rides the text channel.
    assert "screenshot_after_base64" not in payload
    assert "image_base64" not in json.dumps(payload)


async def test_computer_execute_error_shapes_stay_plain_dicts(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Error/rejection/safety shapes keep the legacy plain dict form (never blocks)."""
    session_id, _bundle, _backend, _ = make_session(
        monkeypatch, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    rejected = await server.computer_execute(session_id, "key", keys=["a"])
    assert isinstance(rejected, dict) and rejected["ok"] is False
    # The shipped SafetyPolicy classifies destructive text as safety_denied (a plain
    # dict too); the approval gate shape is pinned in the controller suite. What
    # matters here: NO non-executed path ever returns content blocks.
    denied = await server.computer_execute(session_id, "type", text="rm -rf /")
    assert isinstance(denied, dict) and denied["ok"] is False
    unknown = await server.computer_execute("no-such-session", "wait", delta=1)
    assert isinstance(unknown, dict) and unknown["ok"] is False
    stopped = await server.computer_execute("never-started", "click", x=1, y=1)
    assert isinstance(stopped, dict) and stopped["ok"] is False


async def test_computer_execute_opt_out_returns_slim_text_only(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """include_screenshot_after=False: no ImageContent, no image bytes anywhere.

    Shape: the opt-out keeps the legacy plain dict (byte-compatible with the
    pre-REM-A opt-out response minus the blob) — the content-block form carries an
    image, so the opt-out never uses it.
    """
    session_id, _bundle, backend, _ = make_session(
        monkeypatch, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    result = await server.computer_execute(
        session_id, "click", x=25, y=25, include_screenshot_after=False
    )
    assert isinstance(result, dict)  # legacy opt-out shape, never content blocks
    assert result["ok"] is True
    assert result["verification"]["outcome"] in {"verified", "uncertain", "failed"}
    assert "screenshot_after_base64" not in result
    assert _all_base64_images(result) == []  # no image bytes anywhere, nested included
    assert len(backend.executed) == 1  # the action still ran


# --- H5 + H6: queue hygiene ---------------------------------------------------------------------


async def test_follow_up_results_carry_no_images_and_are_slim(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Queue entries: slim per-item results; the primary action never rides a second copy."""
    session_id, _bundle, backend, _ = make_session(
        monkeypatch, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    result = await server.computer_execute(
        session_id,
        "click",
        x=10,
        y=10,
        expected_effect="the click changes the screen",
        follow_ups=[
            {"action": "click", "x": 20, "y": 20, "expected_effect": "changes again"},
            {"action": "wait", "delta": 1},
        ],
    )
    assert isinstance(result, list) and len(result) == 2
    text_block, image_block = result
    payload = json.loads(text_block.text)
    assert payload["ok"] is True
    assert payload["follow_ups_stopped_reason"] is None
    entries = payload["follow_up_results"]
    assert len(entries) == 3

    primary = entries[0]
    # H6: the primary action NEVER carries a duplicated result blob...
    assert "result" not in primary
    assert "screenshot_after_base64" not in primary
    # ...its essentials ride the TOP-LEVEL payload instead.
    assert primary["index"] == 0 and primary["ok"] is True
    assert primary["action_type"] == "click"

    for entry in entries:
        assert "screenshot_after_base64" not in entry
        assert "result" not in entry
        assert entry["verification_outcome"] in {"verified", "failed", "uncertain", None}
    assert _all_base64_images(payload) == []
    assert len(backend.executed) == 3  # the queue really ran every item


async def test_include_screenshot_after_false_holds_on_follow_ups(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """H5: the opt-out strips images on the queue path too — nested bytes included."""
    session_id, _bundle, backend, _ = make_session(
        monkeypatch, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    result = await server.computer_execute(
        session_id,
        "click",
        x=10,
        y=10,
        include_screenshot_after=False,
        follow_ups=[{"action": "click", "x": 20, "y": 20}, {"action": "wait", "delta": 1}],
    )
    assert isinstance(result, dict)  # opt-out keeps the legacy dict, never blocks
    assert result["ok"] is True
    assert len(result["follow_up_results"]) == 3
    assert _all_base64_images(result) == []  # nothing nested, nothing top-level
    assert len(backend.executed) == 3


# --- H7: outbound image size bound ----------------------------------------------------------------


async def test_oversized_png_reencoded_as_jpeg_under_budget(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An oversized PNG travels outbound as JPEG under CORTEX_RESULT_IMAGE_MAX_KB."""
    monkeypatch.setenv("CORTEX_RESULT_IMAGE_MAX_KB", "4")  # force re-encode of the big PNG
    backend = ScriptedBackend(flip=False)
    backend.observe_override_png = _big_png()  # static screen: no flip, ok stays True
    session_id, _bundle, _b, _ = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    result = await server.computer_execute(session_id, "click", x=10, y=10)
    text_block, image_block = result
    assert image_block.mimeType == "image/jpeg"
    decoded = base64.b64decode(image_block.data, validate=True)
    assert decoded[:3] == b"\xff\xd8\xff"  # JPEG magic
    assert len(decoded) <= 4 * 1024  # under the budget forced via the env knob
    payload = json.loads(text_block.text)
    # A no-expectation click on a static screen verifies as uncertain (pre-existing
    # ladder semantics, out of REM-A scope); what is pinned here is the IMAGE path.
    assert payload["ok"] is False
    assert payload["verification"]["outcome"] == "uncertain"
    assert payload["image_format"] == "image/jpeg"
    # the text channel carries no image bytes
    assert _all_base64_images(json.loads(text_block.text)) == []


async def test_outbound_reencode_never_touches_internal_png(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Internal pipeline stays PNG: capture bytes, verification, checkpoints untouched."""
    monkeypatch.setenv("CORTEX_RESULT_IMAGE_MAX_KB", "4")
    backend = ScriptedBackend(flip=False)
    backend.observe_override_png = _big_png()
    session_id, bundle, _b, _ = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    result = await server.computer_execute(session_id, "click", x=10, y=10)
    _text_block, image_block = result
    assert image_block.mimeType == "image/jpeg"  # outbound was re-encoded...
    # ...but the INTERNAL ExecutionResult (verification + audit source) keeps PNG.
    bundle = server._get_bundle(session_id)
    agent = bundle.agent
    outcome = await agent.run_single(bundle.state, _click_action())
    assert outcome.result is not None
    internal = outcome.result.screenshot_after_base64
    assert internal is not None
    internal_png = base64.b64decode(internal)
    assert internal_png.startswith(b"\x89PNG\r\n\x1a\n")  # internal stayed PNG
    assert internal_png != base64.b64decode(image_block.data)  # outbound differs


def _click_action() -> Any:
    from computer_use_mcp.models import GroundedAction

    return GroundedAction(
        action="click", point={"x": 30, "y": 30}, confidence=1.0, reason="REM-A test"
    )


async def test_small_png_travels_untouched(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REM-A H7 retarget (RC-D10/058): the DEFAULT executed-response image is the
    half-res JPEG, so the in-budget-PNG-passes-untouched contract is pinned on the
    EXPLICIT full-resolution opt-in (include_screenshot_after=True) — the
    documented "full image" path whose H7 budget ladder is unchanged."""
    session_id, _bundle, _backend, _ = make_session(
        monkeypatch, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    result = await server.computer_execute(
        session_id, "click", x=10, y=10, include_screenshot_after=True
    )
    _text_block, image_block = result
    assert image_block.mimeType == "image/png"
    assert base64.b64decode(image_block.data, validate=True).startswith(
        bytes([0x89]) + b"PNG"
    )


def test_result_image_knob_parsing_is_fail_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bogus/negative knob values fall back to the default, never blow up."""
    for bogus in ("not-a-number", "-5", "0", "", "1e999"):
        monkeypatch.setenv("CORTEX_RESULT_IMAGE_MAX_KB", bogus)
        assert server._result_image_max_bytes() > 0
    monkeypatch.delenv("CORTEX_RESULT_IMAGE_MAX_KB", raising=False)
    assert server._result_image_max_bytes() == server.RESULT_IMAGE_MAX_KB_DEFAULT * 1024


# --- H8: observe metadata bounds ocr_text / ui_elements ------------------------------------------


async def test_observe_metadata_caps_ocr_text_and_ui_elements(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lists beyond the cap serialize truncated + an omitted-count field."""
    session_id, _bundle, _backend, _ = make_session(monkeypatch, limits=FAST_LIMITS)
    # Seed an oversized observation through the engine's backend surface: the fake
    # backend's next observe() returns a bloated model; computer_observe must cap it.
    bundle = server._get_bundle(session_id)
    bloated = Observation(
        image_base64=_png(),
        width=64,
        height=48,
        ocr_text=[
            TextRegion(text=f"row-{i}", x=1, y=1, width=10, height=10, confidence=0.9)
            for i in range(60)
        ],
        ui_elements=[{"name": f"btn-{i}", "control_type": "button"} for i in range(60)],
    )

    class BloatedBackend:
        def observe(self) -> Observation:
            return bloated

    original = bundle.agent.observation.backend
    bundle.agent.observation.backend = BloatedBackend()
    try:
        result = server.computer_observe(session_id)
    finally:
        bundle.agent.observation.backend = original
    text_block = result[0]
    payload = json.loads(text_block.text)
    observation = payload["observation"]
    cap = server.OBSERVE_METADATA_ELEMENT_CAP
    assert len(observation["ocr_text"]) == cap
    assert observation["ocr_text_omitted_count"] == 60 - cap
    assert len(observation["ui_elements"]) == cap
    assert observation["ui_elements_omitted_count"] == 60 - cap
    # The full-length model itself is untouched (internal state stays complete).
    assert len(bloated.ocr_text) == 60 and len(bloated.ui_elements) == 60


async def test_observe_metadata_cap_omitted_when_lists_fit(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Short lists serialize in full and carry no omitted-count fields."""
    session_id, _bundle, _backend, _ = make_session(monkeypatch, limits=FAST_LIMITS)
    bundle = server._get_bundle(session_id)
    small = Observation(
        image_base64=_png(),
        width=64,
        height=48,
        ocr_text=[TextRegion(text="ok", x=1, y=1, width=10, height=10, confidence=0.9)],
        ui_elements=[{"name": "OK", "control_type": "button", "focused": True}],
    )

    class SmallBackend:
        def observe(self) -> Observation:
            return small

    original = bundle.agent.observation.backend
    bundle.agent.observation.backend = SmallBackend()
    try:
        result = server.computer_observe(session_id)
    finally:
        bundle.agent.observation.backend = original
    payload = json.loads(result[0].text)
    observation = payload["observation"]
    assert len(observation["ocr_text"]) == 1
    assert len(observation["ui_elements"]) == 1
    assert "ocr_text_omitted_count" not in observation
    assert "ui_elements_omitted_count" not in observation


# --- H1: judge path reuses the encoded base64 ----------------------------------------------------


async def test_provider_judge_reuses_encoded_base64(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The judge path passes the observation's OWN base64 through — no PIL re-encode."""
    from computer_use_mcp import agent as agent_module

    session_id, _bundle, _backend, _ = make_session(
        monkeypatch, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    bundle = server._get_bundle(session_id)
    agent = bundle.agent

    captured: dict[str, str] = {}
    reencodes = {"count": 0}

    class ProbeJudgeProvider(ScriptedProvider):
        def judge_change(
            self, before_b64: str, after_b64: str, expected_effect: str, goal: str | None = None
        ) -> dict[str, Any]:
            captured["before"] = before_b64
            captured["after"] = after_b64
            return {"outcome": "verified", "confidence": 0.9, "reason": "probed"}

    original_encode = agent_module._image_to_base64

    def counting_encode(image: Any) -> str:
        reencodes["count"] += 1
        return original_encode(image)

    monkeypatch.setattr(agent_module, "_image_to_base64", counting_encode)
    agent.provider = ProbeJudgeProvider([])
    before = agent._observe("direct_request")
    after = agent._observe("post_action")
    from computer_use_mcp.verification import VerificationIntent, VerificationKind

    intent = VerificationIntent(
        kind=VerificationKind.MODEL_JUDGE, expected_change=True, expected_effect="something"
    )
    verdict = await agent._provider_judge(intent, before, after)
    assert verdict.outcome == "verified"
    # The judge received the ALREADY-ENCODED base64 verbatim (no decode->re-encode).
    assert captured["before"] == before.image_base64
    assert captured["after"] == after.image_base64
    assert reencodes["count"] == 0  # H1: zero redundant PIL re-encodes on the judge path
