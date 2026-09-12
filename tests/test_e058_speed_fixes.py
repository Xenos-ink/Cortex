"""v0.5.7 remediation tests — the four live-proven
Cortex-side slowness multipliers, each fixed and pinned here:

- queue-premise: queued ``follow_ups`` batches stop only on TRUE staleness — the attached
  window's identity (hwnd + title + bounds) changed/closed since the item's premise.
  Ordinary pixel changes from earlier items (drawing, typing, late canvas renders)
  no longer kill every batch (the live 057 session paid one full model turn per
  action for exactly that). ``CORTEX_QUEUE_STRICT_DIGEST=1`` restores the v0.5.6
  whole-screen digest stop, read lazily.
- action-image: the DEFAULT executed ``computer_execute`` response ships a HALF-RESOLUTION
  JPEG (0.5x, q60 -> 960x540 at 1920x1080, target <= ~60KB typical) so driver
  context stops growing by a 150-230KB PNG per action. The explicit
  ``include_screenshot_after=true`` opt-in stays FULL resolution (its documented
  meaning); ``CORTEX_ACTION_IMAGE_FULL=1`` restores full-res defaults; D1 text mode
  still ships no images at all. ``computer_observe`` / ``computer_screenshot`` are
  untouched (full-resolution budget path).
- alias-launch: a bare ensure_app target ("mspaint") resolves to the Store execution alias
  exactly like "mspaint.exe" so the launch dispatch actually starts the process, and
  ``launched=`` is only claimed when a process really started (never a false claim).
- verdict-honesty: ANY action kind with a stated ``expected_effect`` whose pixel diff is
  zero/sub-threshold degrades to ``uncertain`` (0.4) — never the live session's
  definitive false "failed" (a keypress Enter that committed a Paint shape with a
  0.000000 diff). Uncertain is never success; ok stays False. Bare change
  expectations (no described effect) keep the legacy definitive failure.

Non-e2e: fake backends and synthetic PIL frames only — no real screen, no process
spawn, no MCP server launch.
"""

from __future__ import annotations

import base64
import io
import json
import os
import random
from typing import Any

import pytest
from PIL import Image

from computer_use_mcp import backend as backend_module
from computer_use_mcp import server
from computer_use_mcp.backend import FakeComputerBackend, LocalComputerBackend
from computer_use_mcp.models import WindowInfo
from computer_use_mcp.verification import (
    ScreenshotDiffStrategy,
    VerificationEngine,
    VerificationIntent,
    VerificationKind,
)

from test_controller_integration import (
    FAST_LIMITS,
    ScriptedBackend,
    ScriptedProvider,
    execute_payload,
    executed_summary,
    fresh_server,  # noqa: F401 - pytest fixture (module-scoped server reset)
    make_session,
)

QUEUE_STRICT_DIGEST_ENV = "CORTEX_QUEUE_STRICT_DIGEST"
ACTION_IMAGE_FULL_ENV = "CORTEX_ACTION_IMAGE_FULL"


def _png(color: str = "white", size: tuple[int, int] = (64, 48)) -> str:
    image = Image.new("RGB", size, color)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def _noisy_png_b64(width: int = 1920, height: int = 1080, seed: int = 58) -> str:
    """A worst-case frame: uniform random RGB noise (entropy-incompressible)."""
    rng = random.Random(seed)
    image = Image.frombytes(
        "RGB", (width, height), bytes(rng.getrandbits(8) for _ in range(width * height * 3))
    )
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def _busy_desktop_png_b64(width: int = 1920, height: int = 1080, seed: int = 58) -> str:
    """A realistic busy-desktop stand-in: flat background, app windows, a title-bar
    band, and a noise-textured region — the "typical" frame class the ~60KB target
    speaks to (pure noise is entropy-incompressible for ANY lossy codec at q60)."""
    from PIL import ImageDraw

    rng = random.Random(seed)
    image = Image.new("RGB", (width, height), (240, 240, 242))
    draw = ImageDraw.Draw(image)
    for i in range(40):  # app windows
        x0 = (i * 137) % (width - 160)
        y0 = (i * 211) % (height - 120)
        draw.rectangle([x0, y0, x0 + 140, y0 + 80], fill=((i * 37) % 255, (i * 61) % 255, (i * 97) % 255))
        draw.rectangle([x0, y0, x0 + 140, y0 + 18], fill=(60, 60, 200))
    for x in range(0, width, 8):  # subtle vertical structure
        draw.line([(x, 0), (x, height)], fill=(232, 232, 236))
    noise = Image.frombytes("L", (480, 270), bytes(rng.getrandbits(8) for _ in range(480 * 270)))
    image.paste(noise, (720, 405))  # one textured region (e.g. a photo/canvas)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def _structured_png_b64(width: int = 1920, height: int = 1080) -> str:
    """A structured 1920x1080 frame whose PNG is UNDER the REM-A H7 budget, so the
    full-resolution path travels it untouched (the ladder never engages)."""
    from PIL import ImageDraw

    image = Image.new("RGB", (width, height), (240, 240, 242))
    draw = ImageDraw.Draw(image)
    for i in range(40):
        x0 = (i * 137) % (width - 160)
        y0 = (i * 211) % (height - 120)
        draw.rectangle([x0, y0, x0 + 140, y0 + 80], fill=((i * 37) % 255, (i * 61) % 255, (i * 97) % 255))
        draw.rectangle([x0, y0, x0 + 140, y0 + 18], fill=(60, 60, 200))
    for x in range(0, width, 8):
        draw.line([(x, 0), (x, height)], fill=(232, 232, 236))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def _decode_image(data_b64: str) -> Image.Image:
    image = Image.open(io.BytesIO(base64.b64decode(data_b64, validate=True)))
    image.load()
    return image


async def _run(session_id: str, spec: dict[str, Any]) -> dict[str, Any]:
    return execute_payload(await server.computer_execute(session_id, **spec))


# --- queue-premise: queue batching that works on real desktops -----------------------------------------


class LagRenderBackend(ScriptedBackend):
    """Every execute's visual change lands ONE CAPTURE LATE (real canvas render lag).

    Mirrors the live Paint evidence (D12): an action's post-action capture still
    shows the OLD canvas; the committed stroke appears in a LATER capture. A queued
    item's premise is the previous item's post-action capture, so the fresh validate
    capture differs from the premise on EVERY batch item — the exact shape the
    v0.5.6 whole-screen digest stop turned into one-model-turn-per-action.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(flip=False, **kwargs)
        self._committed = 0  # executes whose visual change is committed
        self._rendered = 0   # commits reflected in the LAST captured frame

    def execute(self, action: Any, stop: Any = None, **kwargs: Any) -> str:
        message = super().execute(action, stop)
        self._committed += 1
        return message

    def observe(self) -> Any:
        observation = super().observe()
        # The capture shows the commits rendered BEFORE it; the pending render lands
        # right after the capture (canvas lag).
        observation.image_base64 = _png("white" if self._rendered % 2 == 0 else "black")
        self._rendered = self._committed
        return observation


async def test_d9_batch_survives_per_item_pixel_changes_window_constant(
    monkeypatch: pytest.MonkeyPatch, fresh_server: Any
) -> None:
    """queue-premise core contract: a 3-item batch where EVERY item's change lands late
    (the premise of each queued item no longer matches the screen pixel-for-pixel)
    completes while the attached window's identity stays constant — all 3 items
    execute, ``follow_ups_stopped_reason`` is None, and each item carries an honest
    verdict (the screen change is real and above threshold -> verified)."""
    monkeypatch.delenv(QUEUE_STRICT_DIGEST_ENV, raising=False)
    backend = LagRenderBackend(
        active_window=WindowInfo(hwnd=1, pid=10, process_name="mspaint.exe", title="Untitled - Paint")
    )
    session_id, _bundle, _backend, _ = make_session(
        monkeypatch, backend=backend, provider=ScriptedProvider([]),
        dry_run=False, require_approval=False, limits=FAST_LIMITS,
    )
    payload = await _run(
        session_id,
        {
            "action": "click",
            "x": 10,
            "y": 10,
            "expected_effect": "the stroke appears on the canvas",
            "follow_ups": [
                {"action": "click", "x": 20, "y": 20, "expected_effect": "the stroke appears"},
                {"action": "click", "x": 30, "y": 30, "expected_effect": "the stroke appears"},
            ],
            "include_screenshot_after": False,
        },
    )
    assert len(executed_summary(backend)) == 3, payload.get("follow_ups_stopped_reason")
    assert payload["follow_ups_stopped_reason"] is None
    assert len(payload["follow_up_results"]) == 3
    assert [item["action_type"] for item in payload["follow_up_results"]] == [
        "click",
        "click",
        "click",
    ]
    # Honest per-item verdicts with a lagging canvas: item 0's own before/after are
    # pixel-identical (its render lands late), so its stated effect degrades to
    # uncertain (verdict-honesty, ok=False — never the old false "failed"); items 1-2 SEE the
    # landed changes (their before/after straddle a render) and verify honestly.
    outcomes = [item["verification_outcome"] for item in payload["follow_up_results"]]
    assert outcomes == ["uncertain", "verified", "verified"], outcomes
    assert payload["follow_up_results"][0]["ok"] is False
    assert payload["follow_up_results"][1]["ok"] is True
    assert payload["follow_up_results"][2]["ok"] is True


async def test_d9_window_title_change_between_items_stops_batch(
    monkeypatch: pytest.MonkeyPatch, fresh_server: Any
) -> None:
    """True staleness still stops: the attached window's TITLE changes between queue
    items (the hwnd+title+bounds identity tuple differs from the item's premise) ->
    the batch stops with ``digest_surprise`` BEFORE executing the stale item."""
    monkeypatch.delenv(QUEUE_STRICT_DIGEST_ENV, raising=False)

    class RenamingBackend(ScriptedBackend):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(flip=True, **kwargs)
            self.set_active_window(
                WindowInfo(hwnd=1, pid=10, process_name="mspaint.exe", title="Untitled - Paint")
            )

        def observe(self) -> Any:
            observation = super().observe()
            if self.executes >= 1:  # the app retitles after the first action
                self.set_active_window(
                    WindowInfo(hwnd=1, pid=10, process_name="mspaint.exe", title="Saved - Paint")
                )
            return observation

    backend = RenamingBackend()
    session_id, _bundle, _backend, _ = make_session(
        monkeypatch, backend=backend, provider=ScriptedProvider([]),
        dry_run=False, require_approval=False, limits=FAST_LIMITS,
    )
    payload = await _run(
        session_id,
        {
            "action": "click",
            "x": 10,
            "y": 10,
            "follow_ups": [
                {"action": "click", "x": 20, "y": 20},
                {"action": "click", "x": 30, "y": 30},
            ],
            "include_screenshot_after": False,
        },
    )
    assert payload["follow_ups_stopped_reason"] == "digest_surprise", payload.get(
        "follow_ups_stopped_reason"
    )
    assert len(executed_summary(backend)) == 1, "the stale item and item 3 never ran"


async def test_d9_strict_digest_env_restores_legacy_whole_screen_stop(
    monkeypatch: pytest.MonkeyPatch, fresh_server: Any
) -> None:
    """``CORTEX_QUEUE_STRICT_DIGEST=1`` restores the v0.5.6 stop: the same lagging
    batch that completes by default flushes at the first pixel change since the
    premise, with the legacy ``digest_surprise`` reason."""
    monkeypatch.setenv(QUEUE_STRICT_DIGEST_ENV, "1")
    backend = LagRenderBackend(
        active_window=WindowInfo(hwnd=1, pid=10, process_name="mspaint.exe", title="Untitled - Paint")
    )
    session_id, _bundle, _backend, _ = make_session(
        monkeypatch, backend=backend, provider=ScriptedProvider([]),
        dry_run=False, require_approval=False, limits=FAST_LIMITS,
    )
    payload = await _run(
        session_id,
        {
            "action": "click",
            "x": 10,
            "y": 10,
            "follow_ups": [{"action": "click", "x": 20, "y": 20}],
            "include_screenshot_after": False,
        },
    )
    assert payload["follow_ups_stopped_reason"] == "digest_surprise"
    assert len(executed_summary(backend)) == 1, "the follow-up was flushed"


async def test_d9_strict_digest_env_toggles_lazily_no_leak_between_batches(
    monkeypatch: pytest.MonkeyPatch, fresh_server: Any
) -> None:
    """stop -> continue -> stop across THREE fresh batches: the knob is re-read at
    each queue decision (lazy, no parse-time caching, no state leak)."""
    reasons: list[str | None] = []
    counts: list[int] = []

    async def one_batch() -> None:
        backend = LagRenderBackend(
            active_window=WindowInfo(
                hwnd=1, pid=10, process_name="mspaint.exe", title="Untitled - Paint"
            )
        )
        session_id, _bundle, _backend, _ = make_session(
            monkeypatch, backend=backend, provider=ScriptedProvider([]),
            dry_run=False, require_approval=False, limits=FAST_LIMITS,
        )
        payload = await _run(
            session_id,
            {
                "action": "click",
                "x": 10,
                "y": 10,
                "follow_ups": [{"action": "click", "x": 20, "y": 20}],
                "include_screenshot_after": False,
            },
        )
        reasons.append(payload["follow_ups_stopped_reason"])
        counts.append(len(executed_summary(backend)))

    monkeypatch.setenv(QUEUE_STRICT_DIGEST_ENV, "1")
    await one_batch()
    monkeypatch.delenv(QUEUE_STRICT_DIGEST_ENV, raising=False)
    await one_batch()
    monkeypatch.setenv(QUEUE_STRICT_DIGEST_ENV, "1")
    await one_batch()
    assert reasons == ["digest_surprise", None, "digest_surprise"], reasons
    assert counts == [1, 2, 1], counts


async def test_d9_identityless_backend_keeps_legacy_digest_probe(
    monkeypatch: pytest.MonkeyPatch, fresh_server: Any
) -> None:
    """When NEITHER the premise nor the fresh capture carries window identity
    (identity-less backend), the whole-screen digest comparison still governs so the
    protection never silently weakens: a late pixel change stops the batch."""
    monkeypatch.delenv(QUEUE_STRICT_DIGEST_ENV, raising=False)
    backend = LagRenderBackend()  # no active_window -> identity-less observations
    session_id, _bundle, _backend, _ = make_session(
        monkeypatch, backend=backend, provider=ScriptedProvider([]),
        dry_run=False, require_approval=False, limits=FAST_LIMITS,
    )
    payload = await _run(
        session_id,
        {
            "action": "click",
            "x": 10,
            "y": 10,
            "follow_ups": [{"action": "click", "x": 20, "y": 20}],
            "include_screenshot_after": False,
        },
    )
    assert payload["follow_ups_stopped_reason"] == "digest_surprise"
    assert len(executed_summary(backend)) == 1


# --- action-image: lighter default action responses ---------------------------------------------------


async def test_d10_default_executed_image_is_half_res_jpeg_within_budget(
    monkeypatch: pytest.MonkeyPatch, fresh_server: Any
) -> None:
    """The default executed response's image is a HALF-RESOLUTION JPEG (960x540 at
    1920x1080). A realistic busy-desktop frame lands far under the ~60KB typical
    target (vs the 150-230KB PNG the live session shipped per action); even a
    worst-case ENTROPY-INCOMPRESSIBLE pure-noise frame stays well under the REM-A H7
    180KB outbound budget (no default response can regress to an oversized payload).
    The additive ``image_scale`` truth marker rides the text payload."""
    monkeypatch.delenv(ACTION_IMAGE_FULL_ENV, raising=False)
    session_id, _bundle, backend, _ = make_session(
        monkeypatch, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    backend.observe_override_png = _busy_desktop_png_b64()
    result = await server.computer_execute(session_id, "click", x=10, y=10)
    assert isinstance(result, list) and len(result) == 2
    text_block, image_block = result
    assert image_block.mimeType == "image/jpeg"
    decoded = _decode_image(image_block.data)
    assert decoded.size == (960, 540)
    busy_bytes = len(base64.b64decode(image_block.data, validate=True))
    assert busy_bytes <= 60_000, busy_bytes  # the "~60KB typical" success criterion
    # Worst case: pure noise (no real screen looks like this; no lossy codec can
    # shrink entropy). Still bounded and far under the legacy per-action weight.
    backend.observe_override_png = _noisy_png_b64()
    result = await server.computer_execute(session_id, "click", x=20, y=20)
    _text_block, image_block = result
    noise_bytes = len(base64.b64decode(image_block.data, validate=True))
    assert noise_bytes <= 150_000, noise_bytes
    payload = json.loads(text_block.text)
    assert payload["image_scale"] == 0.5


async def test_d10_include_screenshot_after_true_returns_full_resolution(
    monkeypatch: pytest.MonkeyPatch, fresh_server: Any
) -> None:
    """The explicit ``include_screenshot_after=true`` opt-in is the documented
    FULL-image path: a 1920x1080 frame whose PNG fits the REM-A H7 budget travels
    UNTOUCHED at full resolution — no half-res downscale on the explicit opt-in."""
    monkeypatch.delenv(ACTION_IMAGE_FULL_ENV, raising=False)
    session_id, _bundle, backend, _ = make_session(
        monkeypatch, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    backend.observe_override_png = _structured_png_b64()
    result = await server.computer_execute(
        session_id, "click", x=10, y=10, include_screenshot_after=True
    )
    _text_block, image_block = result
    assert image_block.mimeType == "image/png"  # in-budget PNG keeps its identity
    assert _decode_image(image_block.data).size == (1920, 1080)


async def test_d10_action_image_full_env_restores_full_res_default(
    monkeypatch: pytest.MonkeyPatch, fresh_server: Any
) -> None:
    """``CORTEX_ACTION_IMAGE_FULL=1`` restores the full-resolution DEFAULT (the
    pre-0.5.7 behavior) — read lazily at response-build time."""
    monkeypatch.setenv(ACTION_IMAGE_FULL_ENV, "1")
    session_id, _bundle, backend, _ = make_session(
        monkeypatch, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    backend.observe_override_png = _structured_png_b64()
    result = await server.computer_execute(session_id, "click", x=10, y=10)
    _text_block, image_block = result
    assert _decode_image(image_block.data).size == (1920, 1080)
    monkeypatch.delenv(ACTION_IMAGE_FULL_ENV, raising=False)
    backend.observe_override_png = _structured_png_b64()
    result = await server.computer_execute(session_id, "click", x=20, y=20)
    _text_block, image_block = result
    assert _decode_image(image_block.data).size == (960, 540)  # lazy re-read: default again


async def test_d10_text_mode_still_ships_no_images(
    monkeypatch: pytest.MonkeyPatch, fresh_server: Any
) -> None:
    """D1 text-mode semantics are untouched by action-image: an executed response in
    ``image_delivery="text"`` mode is the slim dict with NO image block and NO
    screenshot bytes — the half-res default must not leak images into text mode."""
    backend = ScriptedBackend()
    monkeypatch.setattr(server, "_backend_factory", lambda: backend)
    monkeypatch.setattr(server, "_provider_factory", lambda: ScriptedProvider([]))
    response = server.start_session(
        dry_run=False,
        require_approval=False,
        limits=FAST_LIMITS,
        image_delivery="text",
    )
    session_id = str(response["session_id"])
    payload = execute_payload(await server.computer_execute(session_id, "click", x=10, y=10))
    assert isinstance(payload, dict)  # slim dict — never content blocks in text mode
    assert payload["ok"] is True
    assert "screenshot_after_base64" not in json.dumps(payload)
    assert "image/jpeg" not in json.dumps(payload)


def test_d10_half_res_encoder_unit_contract() -> None:
    """Unit pin on the encoder: 0.5x LANCZOS downscale + JPEG q60; the REM-A H7
    outbound budget still governs the result; a failed encode falls back to the
    full-resolution budget ladder (never no image)."""
    noisy = _noisy_png_b64()
    halved = server._half_res_action_image(noisy)
    assert halved is not None
    data_b64, mime = halved
    assert mime == "image/jpeg"
    assert _decode_image(data_b64).size == (960, 540)
    assert len(base64.b64decode(data_b64, validate=True)) <= server._result_image_max_bytes()
    # Garbage input degrades to the budget ladder (None here), never raises.
    assert server._half_res_action_image("not-base64-###") is None


# --- alias-launch: ensure_app bare-name alias resolution ------------------------------------------------


class _LaunchRecordingBackend(ScriptedBackend):
    """ScriptedBackend with the REAL 4-arg execute contract (routes ensure_app
    through FakeComputerBackend.ensure_app, which records ``launched_processes``)."""

    def execute(
        self,
        action: Any,
        stop: Any = None,
        focus_hook: Any = None,
        allow_launch: bool = False,
    ) -> str:
        for hook in self.execute_hooks:
            hook(action)
        if action.action.value == "type" and action.text:
            self.typed_text = action.text
        message = FakeComputerBackend.execute(
            self, action, stop, focus_hook=focus_hook, allow_launch=allow_launch
        )
        self.executes += 1
        return message


async def test_d8_bare_name_resolves_through_store_alias_and_launches(
    monkeypatch: pytest.MonkeyPatch, fresh_server: Any
) -> None:
    """alias-launch alias case: target "mspaint" (bare, no extension) resolves through the
    Store execution alias exactly like "mspaint.exe" -> the launch dispatch records
    the REAL executable name and the NO_INSTANCE payload reports
    ``launched=mspaint.exe`` (a launch that actually happened)."""
    monkeypatch.delenv("CORTEX_ATTACH_OR_LAUNCH", raising=False)
    backend = _LaunchRecordingBackend(flip=False)  # app_windows empty -> NO_INSTANCE
    backend.store_aliases.add("mspaint")
    monkeypatch.setattr(server, "_backend_factory", lambda: backend)
    monkeypatch.setattr(server, "_provider_factory", lambda: ScriptedProvider([]))
    response = server.start_session(
        dry_run=False, require_approval=False, limits=FAST_LIMITS,
        allowed_processes=["mspaint.exe"],
    )
    session_id = str(response["session_id"])
    payload = await _run(session_id, {"action": "ensure_app", "target": "mspaint"})
    assert payload["ok"] is True, payload
    message = str(payload["message"])
    assert message.startswith("NO_INSTANCE"), message
    assert "launch=server" in message, message
    assert "launched=mspaint.exe" in message, message  # the ALIAS's real name
    assert backend.ensure_app_calls == ["mspaint"]
    assert backend.launched_processes == ["mspaint.exe"], "the launch must DISPATCH"


async def test_d8_unresolvable_target_never_claims_launched(
    monkeypatch: pytest.MonkeyPatch, fresh_server: Any
) -> None:
    """alias-launch honesty pin: an UNRESOLVABLE target keeps the bare NO_INSTANCE probe —
    the payload carries NO ``launched=`` claim and nothing is recorded as spawned
    (the live defect's wording implied a launch that never happened)."""
    monkeypatch.delenv("CORTEX_ATTACH_OR_LAUNCH", raising=False)
    backend = _LaunchRecordingBackend(flip=False)
    backend.launch_unresolvable.add("nosuchapp")
    monkeypatch.setattr(server, "_backend_factory", lambda: backend)
    monkeypatch.setattr(server, "_provider_factory", lambda: ScriptedProvider([]))
    response = server.start_session(
        dry_run=False, require_approval=False, limits=FAST_LIMITS,
        allowed_processes=["nosuchapp.exe"],
    )
    session_id = str(response["session_id"])
    payload = await _run(session_id, {"action": "ensure_app", "target": "nosuchapp"})
    assert payload["ok"] is True
    message = str(payload["message"])
    assert message.startswith("NO_INSTANCE"), message
    assert "launched=" not in message, message  # never a false launch claim
    assert backend.launched_processes == []


def test_d8_real_backend_alias_probe_tries_bare_and_exe_names(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """Unit pin on the REAL resolution order (no spawn): the Store-alias probe tries
    ``<needle>`` AND ``<needle>.exe`` under %LOCALAPPDATA%\\Microsoft\\WindowsApps,
    so a bare "mspaint" finds the reparse point that is named WITH its extension."""
    alias_dir = tmp_path / "Microsoft" / "WindowsApps"
    alias_dir.mkdir(parents=True)
    (alias_dir / "mspaint.exe").write_bytes(b"reparse")  # named WITH the extension
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    resolved = LocalComputerBackend._store_alias_path("mspaint")
    assert resolved is not None
    assert resolved.casefold().endswith("mspaint.exe")
    assert LocalComputerBackend._store_alias_path("definitely-missing-app") is None


def test_d8_real_launch_process_reports_actual_spawn_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_launch_process`` returns the basename of the target that ACTUALLY started
    (the honest ``launched=`` name) and ``None`` when the spawn failed — verified
    without spawning anything by stubbing Popen/which on a bare instance."""
    instance = LocalComputerBackend.__new__(LocalComputerBackend)  # no __init__: no DPI side effects
    spawned: list[str] = []

    class _FakePopen:
        def __init__(self, argv: list[str], shell: bool = False) -> None:
            assert shell is False
            spawned.append(argv[0])

    monkeypatch.setattr(backend_module.subprocess, "Popen", _FakePopen)
    monkeypatch.setattr(backend_module.shutil, "which", lambda needle: None)
    monkeypatch.setattr(
        LocalComputerBackend, "_store_alias_path", staticmethod(lambda needle: "C:/alias/mspaint.exe")
    )
    assert instance._launch_process("mspaint") == "mspaint.exe"
    assert spawned == ["C:/alias/mspaint.exe"]

    def _explode(argv: list[str], shell: bool = False) -> None:
        raise FileNotFoundError(argv[0])

    monkeypatch.setattr(backend_module.subprocess, "Popen", _explode)
    assert instance._launch_process("mspaint") is None  # soft failure: honest None


# --- verdict-honesty: honest verdicts for stated effects --------------------------------------------------


async def test_d11_stated_effect_keypress_zero_diff_degrades_to_uncertain(
    monkeypatch: pytest.MonkeyPatch, fresh_server: Any
) -> None:
    """The live D11 evidence: a KEYPRESS with a stated effect on a pixel-identical
    screen (the Enter that committed a Paint shape with sub-threshold dashed
    handles) degrades to UNCERTAIN at 0.4 — never the definitive false "failed".
    Uncertain is never success: ok stays False."""
    monkeypatch.delenv("CORTEX_QUEUE_STRICT_VERIFY", raising=False)
    backend = ScriptedBackend(flip=False)  # every frame identical
    session_id, _bundle, backend, _ = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False,
        limits=FAST_LIMITS,
    )
    payload = await _run(
        session_id,
        {
            "action": "keypress",
            "keys": ["enter"],
            "expected_effect": "the shape is committed",
            "include_screenshot_after": False,
        },
    )
    assert payload["ok"] is False, "uncertain is never success"
    verification = payload["verification"]
    assert verification["outcome"] == "uncertain", verification
    assert abs(verification["confidence"] - 0.4) < 1e-9, verification
    assert verification["verified"] is False
    assert backend.executed, "the input still dispatched (the verdict is about verification)"


async def test_d11_stated_effect_genuine_change_still_verified(
    monkeypatch: pytest.MonkeyPatch, fresh_server: Any
) -> None:
    """The verified path is unchanged: a stated effect with a REAL above-threshold
    screen change is ``verified`` (ok=True) exactly as before."""
    backend = ScriptedBackend(flip=True)  # every execute flips white -> black
    session_id, _bundle, _backend, _ = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False,
        limits=FAST_LIMITS,
    )
    payload = await _run(
        session_id,
        {
            "action": "keypress",
            "keys": ["enter"],
            "expected_effect": "the screen changes",
            "include_screenshot_after": False,
        },
    )
    assert payload["ok"] is True, payload
    assert payload["verification"]["outcome"] == "verified", payload.get("verification")


def test_d11_bare_change_expectation_keeps_legacy_definitive_failed() -> None:
    """Legacy semantics preserved where they are CORRECT: a bare change expectation
    (expected_change=True, NO described effect) with an unchanged screen is a
    definitive ``failed`` at the legacy confidence — the pixel change IS the whole
    claim there, so absent change is the failure (real-defect detection intact)."""
    engine = VerificationEngine()
    before = _png("white")
    intent = VerificationIntent(
        kind=VerificationKind.VISUAL_CHANGE.value,
        expected_change=True,
        metadata={"action_id": "bare", "verification_hint": ""},
    )

    class _Obs:
        pass

    from computer_use_mcp.models import Observation

    def _obs(png: str) -> Observation:
        return Observation(
            image_base64=png,
            width=64,
            height=48,
            active_window=None,
            active_window_info=None,
        )

    result = engine.verify(intent, _obs(before), _obs(before))
    assert result.outcome == "failed", result
    assert result.note == "Expected change was not observed."
    assert result.confidence == 0.85
    assert result.verification_method == "screenshot_diff"
    # And the diff tier directly, on the same evidence class:
    direct = ScreenshotDiffStrategy().verify(intent, _obs(before), _obs(before))
    assert direct.outcome == "failed"
