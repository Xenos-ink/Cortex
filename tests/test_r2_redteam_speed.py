"""Independent red-team for the v0.5.7 fixes.

Authored independently of the fixes; this file ATTACKS them. Every test below is an
adversarial repro aimed at a contract corner the fixer's own suite (test_e058_speed_fixes)
does not cover:

- queue-premise adversarial premises: attached-window MINIMIZE (bounds change), a SECOND
  background window while the attached window stays constant, a mid-batch CLOSE,
  and the STRICT_DIGEST x STRICT_VERIFY crossfire; plus the primary-action legacy
  staleness treatment staying untouched.
- action-image adversarial image budgets: photographic-gradient default weight, the H7
  180KB ladder holding on EVERY path (including a tightened CORTEX_RESULT_IMAGE_MAX_KB),
  FULL-env + explicit opt-in composing without double-encode, text mode emitting zero
  image blocks even when the caller opts in.
- alias-launch adversarial resolution: alias matrix (.exe-only, bare-only, neither), launch
  raising (no false ``launched=``), unresolvable names (no claim, nothing starts).
- verdict-honesty adversarial verdicts: stated-effect HOTKEY/DRAG with zero diff (window change
  must NOT verify them), real-change stated-effect stays verified, bare vs stated
  sub-threshold split, and the fault-injection false-success attack (a stated type
  effect vs an unrelated window + focused-element change must stay ok=False).
- Schema/served-text invariants and the turn-economics capture floor.

Non-e2e: fake backends and synthetic PIL frames only — no real screen, no process
spawn, no MCP server launch, no modification of production code.
"""

from __future__ import annotations

import base64
import inspect
import io
import json
import random
from typing import Any

import pytest
from PIL import Image

from computer_use_mcp import backend as backend_module
from computer_use_mcp import server
from computer_use_mcp.backend import FakeComputerBackend, LocalComputerBackend
from computer_use_mcp.models import GroundedAction, Observation, WindowInfo
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
    audit_events,
    execute_payload,
    executed_summary,
    fresh_server,  # noqa: F401 - pytest fixture (module-scoped server reset)
    make_session,
)

QUEUE_STRICT_DIGEST_ENV = "CORTEX_QUEUE_STRICT_DIGEST"
QUEUE_STRICT_VERIFY_ENV = "CORTEX_QUEUE_STRICT_VERIFY"
ACTION_IMAGE_FULL_ENV = "CORTEX_ACTION_IMAGE_FULL"
RESULT_IMAGE_MAX_KB_ENV = "CORTEX_RESULT_IMAGE_MAX_KB"

ATTACHED = WindowInfo(
    hwnd=1,
    pid=10,
    process_name="mspaint.exe",
    title="Untitled - Paint",
    bounds=(100, 100, 900, 700),
)
MINIMIZED_BOUNDS = (-32000, -32000, 160, 28)  # the real Win32 minimized rect


def _png(color: str = "white", size: tuple[int, int] = (64, 48)) -> str:
    image = Image.new("RGB", size, color)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def _noise_png_b64(width: int = 1920, height: int = 1080, seed: int = 2) -> str:
    """Worst-case outbound frame: entropy-incompressible uniform RGB noise."""
    rng = random.Random(seed)
    image = Image.frombytes(
        "RGB", (width, height), bytes(rng.getrandbits(8) for _ in range(width * height * 3))
    )
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def _gradient_png_b64(width: int = 1920, height: int = 1080) -> str:
    """A smooth photographic-style frame (channel gradients, no flat fill)."""
    small = Image.new("RGB", (16, 9))
    for y in range(9):
        for x in range(16):
            small.putpixel((x, y), (x * 16, y * 28, (x * 7 + y * 13) % 256))
    image = small.resize((width, height), Image.BICUBIC)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def _structured_png_b64(width: int = 1920, height: int = 1080) -> str:
    """A structured (compressible) full-HD frame whose PNG fits the H7 budget."""
    from PIL import ImageDraw

    image = Image.new("RGB", (width, height), (240, 240, 242))
    draw = ImageDraw.Draw(image)
    for i in range(40):
        x0 = (i * 137) % (width - 160)
        y0 = (i * 211) % (height - 120)
        draw.rectangle([x0, y0, x0 + 140, y0 + 80], fill=((i * 37) % 255, (i * 61) % 255, (i * 97) % 255))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def _decode_image(data_b64: str) -> Image.Image:
    image = Image.open(io.BytesIO(base64.b64decode(data_b64, validate=True)))
    image.load()
    return image


async def _run(session_id: str, spec: dict[str, Any]) -> dict[str, Any]:
    return execute_payload(await server.computer_execute(session_id, **spec))


class ShiftingScreenBackend(ScriptedBackend):
    """ScriptedBackend with two adversarial dials:

    - ``canvas_shift``: EVERY capture returns a different frame (the digest between a
      queued item's premise and its validate capture NEVER matches — the exact shape
      that killed every v0.5.6 batch), while the attached window identity stays
      constant unless a ``mutations`` entry says otherwise.
    - ``mutations``: {observe_call_index -> callable(backend)} applied BEFORE that
      capture — the surgical way to minimize/close/steal the attached window at an
      exact point of the capture sequence.
    """

    _SHIFT = ["white", "#f0f0ee", "#e4e4e0", "#d8d8d2", "#cccccc", "#c0c0ba", "#b4b4ac"]

    def __init__(self, *, canvas_shift: bool = False, mutations: dict[int, Any] | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.canvas_shift = canvas_shift
        self.mutations = dict(mutations or {})
        self.observe_calls = 0

    def observe(self) -> Any:
        index = self.observe_calls
        self.observe_calls += 1
        mutation = self.mutations.get(index)
        if mutation is not None:
            mutation(self)
        observation = super().observe()
        if self.canvas_shift:
            observation.image_base64 = _png(self._SHIFT[index % len(self._SHIFT)])
        return observation


# =============================================================================================
# Attack 1 — queue-premise adversarial queue premises
# =============================================================================================


async def test_r2_d9_attached_window_minimize_between_items_stops_batch(
    monkeypatch: pytest.MonkeyPatch, fresh_server: Any
) -> None:
    """The attached window MINIMIZES right after item 0's post capture (its bounds
    become the Win32 minimized rect): the queued item's premise (hwnd+title+bounds)
    no longer matches the fresh validate capture -> the batch must STOP, and the
    stop must name the identity drift. Frames are pixel-identical here, so the stop
    can ONLY be identity-driven (a pure-identity stop, not a digest stop)."""
    monkeypatch.delenv(QUEUE_STRICT_DIGEST_ENV, raising=False)

    def minimize(backend: Any) -> None:
        assert backend.active_window is not None
        backend.active_window = backend.active_window.model_copy(update={"bounds": MINIMIZED_BOUNDS})

    backend = ShiftingScreenBackend(
        flip=False, mutations={3: minimize}, active_window=ATTACHED.model_copy()
    )
    session_id, _bundle, backend, _ = make_session(
        monkeypatch, backend=backend, provider=ScriptedProvider([]),
        dry_run=False, require_approval=False, limits=FAST_LIMITS,
    )
    payload = await _run(
        session_id,
        {
            "action": "click", "x": 10, "y": 10,
            "follow_ups": [{"action": "click", "x": 20, "y": 20}],
            "include_screenshot_after": False,
        },
    )
    assert payload["follow_ups_stopped_reason"] == "digest_surprise", payload.get(
        "follow_ups_stopped_reason"
    )
    assert len(executed_summary(backend)) == 1, "the follow-up must not run against a minimized window"
    entry = payload["follow_up_results"][1]
    assert entry["kind"] == "digest_surprise"
    assert "identity changed" in entry["message"], entry["message"]
    primary = payload["follow_up_results"][0]
    assert primary["kind"] == "executed"


async def test_r2_d9_second_background_window_does_not_stop_batch(
    monkeypatch: pytest.MonkeyPatch, fresh_server: Any
) -> None:
    """A SECOND window opens mid-batch while the ATTACHED window keeps its identity
    (still foreground, same hwnd/title/bounds): ordinary pixel changes from earlier
    items plus a foreign window in the list must NOT stop the batch — the queue
    continues and both items execute. Under the v0.5.6 whole-screen digest stop the
    shifting canvas alone would have killed this batch at item 1."""
    monkeypatch.delenv(QUEUE_STRICT_DIGEST_ENV, raising=False)

    def open_background_window(backend: Any) -> None:
        backend.windows.append(
            WindowInfo(hwnd=7, pid=77, process_name="explorer.exe", title="Foreign File Manager")
        )

    backend = ShiftingScreenBackend(
        flip=False,
        canvas_shift=True,  # every capture differs: the digest probe ALWAYS mismatches
        mutations={3: open_background_window},
        active_window=ATTACHED.model_copy(),
    )
    session_id, _bundle, backend, _ = make_session(
        monkeypatch, backend=backend, provider=ScriptedProvider([]),
        dry_run=False, require_approval=False, limits=FAST_LIMITS,
    )
    payload = await _run(
        session_id,
        {
            "action": "click", "x": 10, "y": 10,
            "follow_ups": [{"action": "click", "x": 20, "y": 20}],
            "include_screenshot_after": False,
        },
    )
    assert payload["follow_ups_stopped_reason"] is None, payload.get("follow_ups_stopped_reason")
    assert len(executed_summary(backend)) == 2
    assert [item["kind"] for item in payload["follow_up_results"]] == ["executed", "executed"]


async def test_r2_d9_attached_window_close_mid_batch_stops_with_honest_reason(
    monkeypatch: pytest.MonkeyPatch, fresh_server: Any
) -> None:
    """The attached window CLOSES right after item 0 (identity present in the premise,
    absent in the fresh capture): the asymmetry is true staleness -> stop, and the
    stop message must say the attached window closed (honest reason, not a generic
    digest complaint)."""
    monkeypatch.delenv(QUEUE_STRICT_DIGEST_ENV, raising=False)
    backend = ShiftingScreenBackend(
        flip=False,
        mutations={3: lambda backend: backend.set_active_window(None)},
        active_window=ATTACHED.model_copy(),
    )
    session_id, _bundle, backend, _ = make_session(
        monkeypatch, backend=backend, provider=ScriptedProvider([]),
        dry_run=False, require_approval=False, limits=FAST_LIMITS,
    )
    payload = await _run(
        session_id,
        {
            "action": "click", "x": 10, "y": 10,
            "follow_ups": [{"action": "click", "x": 20, "y": 20}],
            "include_screenshot_after": False,
        },
    )
    assert payload["follow_ups_stopped_reason"] == "digest_surprise"
    assert len(executed_summary(backend)) == 1
    entry = payload["follow_up_results"][1]
    assert entry["kind"] == "digest_surprise"
    assert "closed" in entry["message"], entry["message"]


async def test_r2_d9_strict_digest_and_strict_verify_crossfire_is_sane(
    monkeypatch: pytest.MonkeyPatch, fresh_server: Any
) -> None:
    """056's STRICT_VERIFY and 058's STRICT_DIGEST set TOGETHER must compose sanely:
    (a) a premise-stale batch stops BEFORE executing the stale item (digest_surprise —
    the premise gate fires first, strict verify never gets a vote); (b) a
    premise-VALID batch (identical pixels) still honors strict verify — the
    launch-prefix item whose window never appears fails DEFINITIVELY and stops the
    queue with verification_failed. Neither knob may disable the other."""
    # (a) both knobs on, premise stale (shifting canvas) -> digest_surprise pre-execution.
    monkeypatch.setenv(QUEUE_STRICT_DIGEST_ENV, "1")
    monkeypatch.setenv(QUEUE_STRICT_VERIFY_ENV, "1")
    backend = ShiftingScreenBackend(
        flip=False, canvas_shift=True, active_window=ATTACHED.model_copy()
    )
    session_id, _bundle, backend, _ = make_session(
        monkeypatch, backend=backend, provider=ScriptedProvider([]),
        dry_run=False, require_approval=False, limits=FAST_LIMITS,
    )
    payload = await _run(
        session_id,
        {
            "action": "click", "x": 10, "y": 10,
            "follow_ups": [{"action": "click", "x": 20, "y": 20}],
            "include_screenshot_after": False,
        },
    )
    assert payload["follow_ups_stopped_reason"] == "digest_surprise"
    assert len(executed_summary(backend)) == 1
    assert payload["follow_up_results"][1]["kind"] == "digest_surprise"

    # (b) both knobs on, premise valid (settled screen) -> strict verify still governs.
    backend2 = ShiftingScreenBackend(
        flip=False, active_window=ATTACHED.model_copy()
    )
    session_id2, _bundle2, backend2, _ = make_session(
        monkeypatch, backend=backend2, provider=ScriptedProvider([]),
        dry_run=False, require_approval=False, limits=FAST_LIMITS,
    )
    payload2 = await _run(
        session_id2,
        {
            "action": "click", "x": 10, "y": 10,
            "follow_ups": [
                {"action": "keypress", "keys": ["enter"], "expected_effect": "open Calculator"},
                {"action": "click", "x": 30, "y": 30},
            ],
            "include_screenshot_after": False,
        },
    )
    assert payload2["follow_ups_stopped_reason"] == "verification_failed", payload2.get(
        "follow_ups_stopped_reason"
    )
    assert len(executed_summary(backend2)) == 2, "the failing item ran; item 3 never did"
    assert payload2["follow_up_results"][1]["verification_outcome"] == "failed"


# =============================================================================================
# Attack 2 — queue-premise must not weaken the PRIMARY action's legacy staleness treatment
# =============================================================================================


async def test_r2_d9_primary_action_stale_premise_keeps_legacy_recovery(
    monkeypatch: pytest.MonkeyPatch, fresh_server: Any
) -> None:
    """A PRIMARY (non-queued) action whose premise went stale mid-call (the foreground
    window's hwnd changed between the direct_request premise capture and the validate
    capture) must get the LEGACY treatment — P0-H STALE_OBSERVATION rejection + the
    automatic re-observe/revalidate recovery — never the D9 identity bypass (scoped to
    queued items only) and never a digest_surprise. The action then executes exactly
    once against the FRESH screen."""
    foreign = WindowInfo(hwnd=2, pid=20, process_name="other.exe", title="Foreign - Other")
    backend = ShiftingScreenBackend(
        flip=True,  # white premise screen, black post-action screen (real change)
        mutations={1: lambda backend: backend.set_active_window(foreign)},
        active_window=ATTACHED.model_copy(),
    )
    session_id, bundle, backend, _ = make_session(
        monkeypatch, backend=backend, provider=ScriptedProvider([]),
        dry_run=False, require_approval=False, limits=FAST_LIMITS,
    )
    payload = await _run(session_id, {"action": "click", "x": 10, "y": 10})
    # Never a queue-digest outcome on the primary path:
    assert payload.get("error") != "digest_surprise", payload
    assert payload.get("follow_ups_stopped_reason") is None
    # The legacy staleness gate fired and the P0-H recovery re-observed: the capture
    # sequence shows the revalidate phase (the same evidence the fault-injection pin
    # uses); the D9 staleness reason never appears anywhere in the validation audits.
    events = audit_events(bundle, session_id)
    phases = [
        (event.get("metadata") or {}).get("phase")
        for event in events
        if event["event_type"] == "observation"
    ]
    assert phases == ["direct_request", "validate", "revalidate", "post_action"], phases
    assert not any(
        (event.get("metadata") or {}).get("reason", "").startswith("Post-action staleness")
        for event in events
        if event["event_type"] == "validation"
    ), "the primary path must never emit the queue-premise staleness reason"
    # The action still ran exactly once, against the fresh screen, and verified.
    assert payload["ok"] is True, payload
    assert len(executed_summary(backend)) == 1


# =============================================================================================
# Attack 3 — action-image adversarial image budgets
# =============================================================================================


async def test_r2_d10_photographic_gradient_default_response_under_100kb(
    monkeypatch: pytest.MonkeyPatch, fresh_server: Any
) -> None:
    """A photographic-gradient 1920x1080 frame (smoother than a busy desktop, rougher
    than a flat one) ships as a 0.5x JPEG well under 100KB on the DEFAULT path — the
    context-weight contract holds for the realistic mid-band of screen content."""
    monkeypatch.delenv(ACTION_IMAGE_FULL_ENV, raising=False)
    session_id, _bundle, backend, _ = make_session(
        monkeypatch, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    backend.observe_override_png = _gradient_png_b64()
    result = await server.computer_execute(session_id, "click", x=10, y=10)
    assert isinstance(result, list) and len(result) == 2
    text_block, image_block = result
    assert image_block.mimeType == "image/jpeg"
    decoded = _decode_image(image_block.data)
    assert decoded.size == (960, 540)
    raw_bytes = len(base64.b64decode(image_block.data, validate=True))
    assert raw_bytes <= 100_000, raw_bytes
    payload = json.loads(text_block.text)
    assert payload["image_scale"] == 0.5
    assert payload["image_format"] == "image/jpeg"


async def test_r2_d10_h7_budget_holds_on_every_path_even_tightened(
    monkeypatch: pytest.MonkeyPatch, fresh_server: Any
) -> None:
    """The JPEG re-encode must never exceed the H7 ladder budget on ANY path: with a
    pure-noise frame (worst case) BOTH the default half-res path and the explicit
    full-res path stay under the configured CORTEX_RESULT_IMAGE_MAX_KB — including a
    deliberately tightened 64KB budget (the half-res q60 output itself gets laddered)."""
    monkeypatch.delenv(ACTION_IMAGE_FULL_ENV, raising=False)
    monkeypatch.setenv(RESULT_IMAGE_MAX_KB_ENV, "64")
    budget = server._result_image_max_bytes()
    assert budget == 64 * 1024
    session_id, _bundle, backend, _ = make_session(
        monkeypatch, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    backend.observe_override_png = _noise_png_b64()
    # Default path: half-res JPEG, bounded by the tightened budget. For this
    # pathological frame the q60 half-res output itself exceeds 64KB, so the H7
    # ladder engages and shrinks it further — the CONTRACT is the budget, held.
    result = await server.computer_execute(session_id, "click", x=10, y=10)
    _text_block, image_block = result
    raw_bytes = len(base64.b64decode(image_block.data, validate=True))
    assert raw_bytes <= budget, (image_block.mimeType, raw_bytes)
    assert image_block.mimeType == "image/jpeg"
    assert 0 < _decode_image(image_block.data).size[0] <= 960  # never up-scaled, bounded
    # Explicit full path with the same impossible frame: the ladder still governs.
    backend.observe_override_png = _noise_png_b64(seed=3)
    result = await server.computer_execute(
        session_id, "click", x=20, y=20, include_screenshot_after=True
    )
    _text_block, image_block = result
    raw_bytes = len(base64.b64decode(image_block.data, validate=True))
    assert raw_bytes <= budget, (image_block.mimeType, raw_bytes)
    assert image_block.mimeType == "image/jpeg"


async def test_r2_d10_full_env_plus_explicit_optin_no_double_encode(
    monkeypatch: pytest.MonkeyPatch, fresh_server: Any
) -> None:
    """CORTEX_ACTION_IMAGE_FULL=1 AND include_screenshot_after=true together: the
    full-res path wins, and an in-budget PNG travels BYTE-IDENTICAL (no re-encode,
    no double-compress weirdness, no image_scale marker on the full path)."""
    monkeypatch.setenv(ACTION_IMAGE_FULL_ENV, "1")
    session_id, _bundle, backend, _ = make_session(
        monkeypatch, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    structured = _structured_png_b64()
    assert len(base64.b64decode(structured, validate=True)) <= server._result_image_max_bytes()
    backend.observe_override_png = structured
    result = await server.computer_execute(
        session_id, "click", x=10, y=10, include_screenshot_after=True
    )
    text_block, image_block = result
    assert image_block.mimeType == "image/png"
    assert image_block.data == structured, "an in-budget PNG must travel untouched"
    assert _decode_image(image_block.data).size == (1920, 1080)
    payload = json.loads(text_block.text)
    assert payload["image_format"] == "image/png"
    assert "image_scale" not in payload, "the half-res truth marker must not lie on the full path"


async def test_r2_d10_text_mode_with_explicit_optin_zero_image_blocks(
    monkeypatch: pytest.MonkeyPatch, fresh_server: Any
) -> None:
    """Text mode + include_screenshot_after=true: the caller can NEVER re-enable
    images — the response stays the slim dict with zero image blocks and zero
    screenshot bytes even though the capture succeeded."""
    backend = ScriptedBackend()
    monkeypatch.setattr(server, "_backend_factory", lambda: backend)
    monkeypatch.setattr(server, "_provider_factory", lambda: ScriptedProvider([]))
    response = server.start_session(
        dry_run=False, require_approval=False, limits=FAST_LIMITS, image_delivery="text",
    )
    session_id = str(response["session_id"])
    payload = await _run(
        session_id, {"action": "click", "x": 10, "y": 10, "include_screenshot_after": True}
    )
    assert isinstance(payload, dict), "text mode must never return content blocks"
    assert payload["ok"] is True
    dumped = json.dumps(payload)
    assert "screenshot_after_base64" not in dumped
    assert "image/jpeg" not in dumped and "image/png" not in dumped
    assert payload["image_delivery"] == "text"


# =============================================================================================
# Attack 4 — alias-launch adversarial alias resolution and launch honesty
# =============================================================================================


def test_r2_d8_alias_resolution_matrix_exe_only_bare_only_neither(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """Resolution matrix against the REAL probe: a name existing ONLY as .exe resolves
    for BOTH the bare and the .exe needle; a name existing ONLY as a bare alias
    resolves for the bare needle (and honestly None for the .exe needle — there is no
    such reparse point); a name in NEITHER form resolves to None; no LOCALAPPDATA
    degrades to None (never a spawn target)."""
    # (a) .exe-only alias (the live mspaint shape).
    exe_dir = tmp_path / "exe" / "Microsoft" / "WindowsApps"
    exe_dir.mkdir(parents=True)
    (exe_dir / "tool.exe").write_bytes(b"reparse")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "exe"))
    resolved = LocalComputerBackend._store_alias_path("tool")
    assert resolved is not None and resolved.casefold().endswith("tool.exe")
    assert LocalComputerBackend._store_alias_path("tool.exe") == resolved
    # (b) bare-only alias (no extension on disk).
    bare_dir = tmp_path / "bare" / "Microsoft" / "WindowsApps"
    bare_dir.mkdir(parents=True)
    (bare_dir / "toolx").write_bytes(b"reparse")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "bare"))
    resolved_bare = LocalComputerBackend._store_alias_path("toolx")
    assert resolved_bare is not None and resolved_bare.casefold().endswith("toolx")
    assert LocalComputerBackend._store_alias_path("toolx.exe") is None  # honest: no such alias
    # (c) neither form.
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "bare"))
    assert LocalComputerBackend._store_alias_path("definitely-missing") is None
    # (d) no LOCALAPPDATA at all.
    monkeypatch.setenv("LOCALAPPDATA", "")
    assert LocalComputerBackend._store_alias_path("tool") is None


def test_r2_d8_launch_raise_returns_none_and_never_records_spawn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """When the launch call RAISES after resolution (the alias path exists but the
    spawn fails), ``_launch_process`` returns None (the payload keeps NO ``launched=``
    claim) and nothing is recorded as started."""
    instance = LocalComputerBackend.__new__(LocalComputerBackend)  # no __init__: no DPI side effects
    spawned: list[str] = []

    class _ExplodingPopen:
        def __init__(self, argv: list[str], shell: bool = False) -> None:
            assert shell is False
            spawned.append(argv[0])
            raise PermissionError(f"access denied: {argv[0]}")

    alias_dir = tmp_path / "Microsoft" / "WindowsApps"
    alias_dir.mkdir(parents=True)
    (alias_dir / "app.exe").write_bytes(b"reparse")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(backend_module.subprocess, "Popen", _ExplodingPopen)
    monkeypatch.setattr(backend_module.shutil, "which", lambda needle: None)
    assert instance._launch_process("app") is None  # the launch failed -> honest None
    assert instance._launch_process("app.exe") is None
    assert spawned == [str(alias_dir / "app.exe"), str(alias_dir / "app.exe")]
    # The REAL ensure_app payload gate: a None launch result keeps ``launched=`` absent;
    # a real result names the started executable. (No desktop contact: both probes are
    # class-level stubs, so the real payload construction at backend.py runs in isolation.)
    monkeypatch.setattr(LocalComputerBackend, "enumerate_app_windows", lambda self, needle: [])
    monkeypatch.setattr(LocalComputerBackend, "_launch_process", lambda self, needle: None)
    payload_none = instance.ensure_app("app", allow_launch=True)
    assert payload_none.startswith("NO_INSTANCE"), payload_none
    assert "launched=" not in payload_none, payload_none
    monkeypatch.setattr(LocalComputerBackend, "_launch_process", lambda self, needle: "app.exe")
    payload_started = instance.ensure_app("app", allow_launch=True)
    assert "launched=app.exe" in payload_started, payload_started


def test_r2_d8_unresolvable_name_no_claim_and_nothing_starts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """An unresolvable name produces NO ``launched=`` claim and starts NOTHING. The
    pre-existing REM-E raw-name fallback attempt is preserved (documented), but it
    fails exactly like CreateProcess does on a real machine (FileNotFoundError) and
    the outcome is the honest soft-None degrade."""
    instance = LocalComputerBackend.__new__(LocalComputerBackend)
    alias_dir = tmp_path / "Microsoft" / "WindowsApps"
    alias_dir.mkdir(parents=True)  # EMPTY: nothing resolvable
    attempted: list[str] = []

    def _real_like_popen(argv: list[str], shell: bool = False) -> None:
        assert shell is False
        attempted.append(argv[0])
        raise FileNotFoundError(f"[WinError 2] The system cannot find the file specified: {argv[0]}")

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(backend_module.subprocess, "Popen", _real_like_popen)
    monkeypatch.setattr(backend_module.shutil, "which", lambda needle: None)
    assert instance._launch_process("nosuchapp-xyz") is None
    assert instance._launch_process("nosuchapp-xyz.exe") is None
    # Only the raw-name fallback was attempted (and failed): no resolution, no spawn.
    assert attempted == ["nosuchapp-xyz", "nosuchapp-xyz.exe"]
    # Probe-level honesty: unresolvable target -> bare NO_INSTANCE, no claim, empty ledger.
    fake = FakeComputerBackend()
    fake.launch_unresolvable.add("nosuchapp")
    for target in ("nosuchapp", "NOSUCHAPP.EXE"):
        payload = fake.ensure_app(target, allow_launch=True)
        assert payload.startswith("NO_INSTANCE"), payload
        assert "launched=" not in payload, payload
    assert fake.launched_processes == []


# =============================================================================================
# Attack 5 — verdict-honesty adversarial verdicts
# =============================================================================================


async def _stated_effect_zero_diff_window_swap_case(
    monkeypatch: pytest.MonkeyPatch,
    spec: dict[str, Any],
) -> dict[str, Any]:
    """Shared rig: stated-effect NON-CLICK action on a pixel-identical screen while an
    unrelated dialog STEALS the foreground mid-action (window identity AND a focused
    UI element change). FocusChangeStrategy must stay click-scoped: none of the
    deterministic transition signals may verify these action kinds."""
    dialog = WindowInfo(hwnd=2, pid=99, process_name="app.exe", title="Confirm Delete?")
    backend = ScriptedBackend(
        flip=False,  # pixels never change
        active_window=WindowInfo(hwnd=1, pid=10, process_name="app.exe", title="Main"),
    )

    def spawn_dialog(action: GroundedAction) -> None:
        backend.set_active_window(dialog)

    backend.execute_hooks.append(spawn_dialog)

    original_observe = backend.observe

    def observe_with_focused_element() -> Any:
        observation = original_observe()
        if backend.active_window is dialog:  # the after-capture: a FOCUSED foreign control
            observation.ui_elements = [
                {"name": "Delete", "control_type": "Button", "focused": True}
            ]
        return observation

    backend.observe = observe_with_focused_element  # type: ignore[method-assign]
    session_id, _bundle, backend, _ = make_session(
        monkeypatch, backend=backend, provider=ScriptedProvider([]),
        dry_run=False, require_approval=False, limits=FAST_LIMITS,
    )
    payload = await _run(session_id, spec)
    verification = payload["verification"]
    assert payload["ok"] is False, (payload, "a window/element change must not verify a non-click effect")
    assert verification["outcome"] == "uncertain", verification
    assert verification["verified"] is False
    assert abs(verification["confidence"] - 0.4) < 1e-9, verification
    # The pixel tier is where the verdict was decided (never a deterministic window /
    # focus signal claiming success for a non-click kind).
    assert "screenshot_diff" in verification["verification_method"], verification
    assert len(executed_summary(backend)) == 1, "the input dispatched exactly once"
    return payload


async def test_r2_d11_hotkey_zero_diff_with_window_swap_is_uncertain(
    monkeypatch: pytest.MonkeyPatch, fresh_server: Any
) -> None:
    """Stated-effect HOTKEY + zero pixel diff + an unrelated foreground steal: the
    verdict degrades to uncertain 0.4 (never the live session's definitive false
    'failed'; never a false success from the window change)."""
    await _stated_effect_zero_diff_window_swap_case(
        monkeypatch,
        {"action": "hotkey", "keys": ["ctrl", "s"], "expected_effect": "the toolbar flashes",
         "include_screenshot_after": False},
    )


async def test_r2_d11_drag_zero_diff_with_window_swap_is_uncertain(
    monkeypatch: pytest.MonkeyPatch, fresh_server: Any
) -> None:
    """Stated-effect DRAG + zero pixel diff + an unrelated foreground steal: uncertain
    0.4 — the drag contract shares the hotkey doctrine."""
    await _stated_effect_zero_diff_window_swap_case(
        monkeypatch,
        {"action": "drag", "x": 5, "y": 5, "x2": 40, "y2": 30,
         "expected_effect": "the marquee stretches", "include_screenshot_after": False},
    )


async def test_r2_d11_real_change_with_stated_effect_drag_still_verified(
    monkeypatch: pytest.MonkeyPatch, fresh_server: Any
) -> None:
    """The verified path is intact for the extended kinds: a stated-effect DRAG over a
    REAL above-threshold change is verified (ok=True) exactly as before — the doctrine
    degraded only the ABSENT-evidence case."""
    backend = ScriptedBackend(flip=True)  # every execute flips white -> black
    session_id, _bundle, _backend, _ = make_session(
        monkeypatch, backend=backend, provider=ScriptedProvider([]),
        dry_run=False, require_approval=False, limits=FAST_LIMITS,
    )
    payload = await _run(
        session_id,
        {"action": "drag", "x": 5, "y": 5, "x2": 40, "y2": 30,
         "expected_effect": "the marquee stretches", "include_screenshot_after": False},
    )
    assert payload["ok"] is True, payload
    assert payload["verification"]["outcome"] == "verified", payload.get("verification")


def test_r2_d11_subthreshold_diff_bare_fails_while_stated_degrades() -> None:
    """The sharpest split: the SAME sub-threshold evidence (16 strongly-changed pixels
    — above zero, below both the mean threshold and the 50-pixel floor) yields the
    legacy definitive ``failed`` 0.85 for a BARE change expectation and ``uncertain``
    0.4 once an effect is STATED. Only ``expected_effect`` differs."""
    engine = VerificationEngine()
    from PIL import ImageDraw

    base = Image.new("RGB", (64, 48), "white")
    draw = ImageDraw.Draw(base)
    draw.rectangle([10, 10, 13, 13], fill=(155, 155, 155))  # 4x4 = 16 px, delta 100
    buffer = io.BytesIO()
    base.save(buffer, format="PNG")
    after_b64 = base64.b64encode(buffer.getvalue()).decode("ascii")

    def _obs(png: str) -> Observation:
        return Observation(image_base64=png, width=64, height=48)

    before = _obs(_png("white"))
    after = _obs(after_b64)
    # The evidence IS sub-threshold (not zero): prove the tier sees it as no-change.
    mean_diff, strongly_changed = _diff_magnitude_probe(before, after)
    assert 0.0 < mean_diff < 1.0 and 0 < strongly_changed < 50, (mean_diff, strongly_changed)

    bare = VerificationIntent(
        kind=VerificationKind.VISUAL_CHANGE.value,
        expected_change=True,
        metadata={"action_id": "bare", "verification_hint": ""},
    )
    bare_result = engine.verify(bare, before, after)
    assert bare_result.outcome == "failed", bare_result
    assert bare_result.note == "Expected change was not observed."
    assert abs(bare_result.confidence - 0.85) < 1e-9

    stated = VerificationIntent(
        kind=VerificationKind.VISUAL_CHANGE.value,
        expected_change=True,
        expected_effect="the shape is committed",
        metadata={"action_id": "stated", "verification_hint": ""},
    )
    stated_result = engine.verify(stated, before, after)
    assert stated_result.outcome == "uncertain", stated_result
    assert stated_result.verified is False
    assert abs(stated_result.confidence - 0.4) < 1e-9, stated_result


def _diff_magnitude_probe(before: Observation, after: Observation) -> tuple[float, int]:
    """Direct probe of the diff tier's magnitudes (decoded frames, no stash)."""
    before_image = ScreenshotDiffStrategy._decode(before.image_base64)
    after_image = ScreenshotDiffStrategy._decode(after.image_base64)
    from computer_use_mcp.verification import _diff_magnitude

    return _diff_magnitude(before_image, after_image)


async def test_r2_d11_fault_injection_window_and_focus_change_must_not_verify_type_effect(
    monkeypatch: pytest.MonkeyPatch, fresh_server: Any
) -> None:
    """The worst-outcome attack: a TYPE action with a stated effect, zero pixel diff,
    while an unrelated dialog steals BOTH the foreground identity AND the focused UI
    element. The flag stays click-scoped, so none of the deterministic transition
    signals may 'verify' the type effect: ok stays False (uncertain), never silently
    successful."""
    payload = await _stated_effect_zero_diff_window_swap_case(
        monkeypatch,
        {"action": "type", "text": "record name", "expected_effect": "the record is deleted",
         "include_screenshot_after": False},
    )
    # The dialog's focused element and new identity produced NO deterministic verified
    # claim anywhere in the evidence (the ok=False / uncertain asserts already held).


# =============================================================================================
# Attack 6 — schema + served-text invariants
# =============================================================================================

ALL_TOOLS = (
    "start_session",
    "stop_session",
    "computer_observe",
    "computer_screenshot",
    "computer_execute",
)


async def test_r2_surface_is_exactly_five_plain_tools() -> None:
    """Exactly 5 registered tools; every advertised inputSchema contains ZERO anyOf
    and ZERO $ref/$defs (the wire-compat contract, re-walked independently)."""
    tools = await server.mcp.list_tools()
    assert sorted(tool.name for tool in tools) == sorted(ALL_TOOLS)
    for name in ALL_TOOLS:
        registered = server.mcp._tool_manager.get_tool(name)
        assert registered is not None, name
        schema_text = json.dumps(registered.fn_metadata.arg_model.model_json_schema())
        assert "anyOf" not in schema_text, name
        assert "$ref" not in schema_text and "$defs" not in schema_text, name


def test_r2_served_computer_execute_text_teaches_d9_and_d10(fresh_server: Any) -> None:
    """The SERVED computer_execute text (what a driving model actually reads) must
    teach BOTH fixes: the half-size JPEG default (action-image) and the
    queue-continues-while-the-attached-window-is-constant semantics (queue-premise)."""
    doc = inspect.getdoc(server.computer_execute) or ""
    assert "HALF-SIZE JPEG" in doc, "served text must teach the action-image default weight"
    assert "0.5x" in doc and "60KB" in doc
    assert "include_screenshot_after=true" in doc  # the full-res opt-in is documented
    assert "CORTEX_ACTION_IMAGE_FULL=1" in doc  # the escape is documented
    assert "while the attached window stays the same" in doc, (
        "served text must teach the queue-premise continue semantics"
    )
    assert "hwnd/title/bounds" in doc, "served text must name the true-staleness identity"
    assert "CORTEX_QUEUE_STRICT_DIGEST=1" in doc  # the legacy escape is documented


# =============================================================================================
# Attack 7 — turn economics: D9/D10 must not add captures per action
# =============================================================================================


class CountingObserveBackend(ScriptedBackend):
    """Counts every capture so the per-action capture floor is pinnable."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.capture_count = 0

    def observe(self) -> Any:
        self.capture_count += 1
        return super().observe()


async def test_r2_capture_floor_unchanged_by_d9_d10(
    monkeypatch: pytest.MonkeyPatch, fresh_server: Any
) -> None:
    """Turn-economics pin: a single action costs exactly 3 captures (direct_request
    premise + validate + post_action) and a 2-item batch costs exactly 5 (the queued
    item reuses the previous post_action capture as its premise — D9's validity check
    consumes the ALREADY-TAKEN validate capture and adds none; D10 is response-side
    encode only). Any NEW per-action capture breaks these exact counts."""
    monkeypatch.delenv(QUEUE_STRICT_DIGEST_ENV, raising=False)
    backend = CountingObserveBackend(flip=False, active_window=ATTACHED.model_copy())
    session_id, _bundle, backend, _ = make_session(
        monkeypatch, backend=backend, provider=ScriptedProvider([]),
        dry_run=False, require_approval=False, limits=FAST_LIMITS,
    )
    await _run(session_id, {"action": "click", "x": 10, "y": 10})
    assert backend.capture_count == 3, backend.capture_count

    backend2 = CountingObserveBackend(flip=False, active_window=ATTACHED.model_copy())
    session_id2, _bundle2, backend2, _ = make_session(
        monkeypatch, backend=backend2, provider=ScriptedProvider([]),
        dry_run=False, require_approval=False, limits=FAST_LIMITS,
    )
    payload = await _run(
        session_id2,
        {
            "action": "click", "x": 10, "y": 10,
            "follow_ups": [{"action": "click", "x": 20, "y": 20}],
            "include_screenshot_after": False,
        },
    )
    assert payload["follow_ups_stopped_reason"] is None
    assert backend2.capture_count == 5, backend2.capture_count
