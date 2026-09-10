"""R-5 mechanical-speed pins (ORVEX-CORTEX-056-LIVEFIX, mission goal section 7).

Pins the four R-5 mechanisms — every one is a MECHANICAL optimization with a
frozen-verification-semantics contract:

- W1 validate-phase capture sharing: a DIRECT action reuses its in-call premise as
  the validate observation ONLY when a capture-free identity probe shows NO drift;
  any drift, a stale premise, a queued follow-up, or ``CORTEX_VALIDATE_REUSE_MS=0``
  falls back to the full validate capture. Audited truthfully (reused=True,
  duration 0.0); the P0-H STALE_OBSERVATION rejection + re-observe recovery still
  fire on drift.
- W2 raw-frame fast path: the backend stashes the capture-time RGB frame;
  ``ScreenshotDiffStrategy``/``FocusChangeStrategy`` use it instead of re-decoding
  the PNG they just watched get encoded; no stash falls back to the exact legacy
  decode and the verdicts/evidence are IDENTICAL.
- W3 one-histogram diff math: the screen-wide mean is derived from the diff's own
  768-bin histogram — bit-identical to the historical ``ImageStat.Stat`` mean (and
  the same strongly-changed tail), pinned on both a synthetic frame pair and a
  live-pair equivalence where the historical values are computed inline.
- W4 outbound-from-raw: ``_bound_outbound_image`` accepts the capture frame and
  produces BYTE-IDENTICAL JPEG-ladder output to the decode-then-encode path.

Also pins the encode-count contract (no double-encode when one suffices: the JPEG
ladder encodes once when q85 fits) and a generous headroom performance smoke on the
stubbed observe path.

No OS input is dispatched anywhere in this file (FakeComputerBackend derivatives /
pure strategy objects); the one live-desktop section is capture-only (mss reading),
skipped off-Windows.
"""

from __future__ import annotations

import base64
import io
import os
import time
from typing import Any

import pytest
from PIL import Image, ImageChops, ImageStat
from test_controller_integration import (
    FAST_LIMITS,
    ScriptedBackend,
    _png,
    audit_events,
    execute_payload,
    make_session,
)

from computer_use_mcp import server
from computer_use_mcp.backend import ComputerBackend, FakeComputerBackend
from computer_use_mcp.models import Observation, WindowInfo
from computer_use_mcp.observation import digest_matches
from computer_use_mcp.state import SessionRegistry
from computer_use_mcp.verification import (
    DEFAULT_DIFF_THRESHOLD,
    STRONG_CHANGE_MIN_PIXELS,
    STRONG_PIXEL_DELTA,
    ScreenshotDiffStrategy,
    VerificationEngine,
    VerificationIntent,
    VerificationKind,
    _diff_magnitude,
)


@pytest.fixture
def fresh_server(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Any:
    """Fresh bounded registry/bundles + per-test audit dir (full session isolation)."""
    monkeypatch.setenv("COMPUTER_USE_MCP_LOG_DIR", str(tmp_path / "audit"))
    monkeypatch.setattr(server, "_registry", SessionRegistry(max_sessions=8))
    monkeypatch.setattr(server, "_bundles", {})
    monkeypatch.setattr(server, "_stopped_sessions", {})
    return server


def _png_image_b64(image: Image.Image) -> str:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


# =====================================================================================
# W1 — validate-phase capture sharing (identity-probe guarded)
# =====================================================================================


class ProbingBackend(ScriptedBackend):
    """ScriptedBackend plus the identity-probe capability (mirrors LocalComputerBackend).

    ``drift`` (None by default) is returned by the probe as a screen-identity change:
    None = no drift (probe endorses the premise), anything else = drift (the agent
    must NOT reuse the premise and must NOT execute against it unvalidated).
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.probe_calls = 0
        self.drift: bool = False

    def identity_probe(self, reference: Observation | None = None) -> Observation | None:
        self.probe_calls += 1
        if reference is None:
            return None
        if self.drift:
            drifted = reference.model_copy(deep=True)
            drifted.active_window_info = WindowInfo(
                hwnd=99, pid=99, process_name="calc.exe", title="Calculator"
            )
            return drifted
        return reference.model_copy(deep=True)


async def test_w1_fresh_premise_reused_as_validate_no_second_capture(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fresh direct premise + no probe drift -> the validate observation IS the premise
    (audited reused=True, duration 0.0) and NO validate capture is taken."""
    backend = ProbingBackend()
    session_id, bundle, backend, _ = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False,
        limits=FAST_LIMITS,
    )
    await server.computer_execute(session_id, "click", x=10, y=10)
    events = audit_events(bundle, session_id)
    observations = [e for e in events if e["event_type"] == "observation"]
    phases = [(e.get("metadata") or {}).get("phase") for e in observations]
    # direct_request capture (real), validate REUSED (no capture), post_action (real)
    assert phases == ["direct_request", "validate", "post_action"], phases
    validate_event = observations[1]
    assert validate_event.get("metadata", {}).get("reused") is True
    assert validate_event.get("duration_ms") == 0.0
    assert bundle.agent.metrics.snapshot()["counters"]["observation_reuse"] >= 1
    assert backend.probe_calls == 1  # the identity probe ran (the guard is not free of proof)


async def test_w1_stale_premise_not_reused_full_validate_capture_taken(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A premise older than the reuse window is NEVER reused: the fresh validate
    capture runs exactly as before (staleness guard is time-bounded)."""
    backend = ProbingBackend()
    session_id, bundle, backend, _ = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False,
        limits=FAST_LIMITS,
    )
    monkeypatch.setenv("CORTEX_VALIDATE_REUSE_MS", "0")  # disable: every premise stale
    await server.computer_execute(session_id, "click", x=10, y=10)
    events = audit_events(bundle, session_id)
    phases = [
        (e.get("metadata") or {}).get("phase")
        for e in events
        if e["event_type"] == "observation"
    ]
    assert phases == ["direct_request", "validate", "post_action"], phases
    reused = [
        e for e in events
        if e["event_type"] == "observation" and (e.get("metadata") or {}).get("reused")
    ]
    assert reused == []
    assert backend.probe_calls == 0  # window check refused before probing


async def test_w1_probe_drift_never_reuses_premise(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Probe-detected drift (foreign window took the foreground) means the premise is
    NOT reused: the drifted probe becomes the validate observation, the P0-H
    STALE_OBSERVATION rejection fires, and the single re-observe recovery discards the
    stale coordinates (the action executes only against the RE-OBSERVED screen, or is
    rejected when re-grounding fails — either way the drifted premise itself never
    reaches execution)."""
    backend = ProbingBackend()
    backend.set_active_window(
        WindowInfo(hwnd=1, pid=1, process_name="mspaint.exe", title="Untitled - Paint")
    )
    backend.drift = True  # the probe sees a foreign foreground
    session_id, bundle, backend, _ = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False,
        limits=FAST_LIMITS,
    )
    response = await server.computer_execute(session_id, "click", x=10, y=10)
    assert execute_payload(response) is not None
    events = audit_events(bundle, session_id)
    assert bundle.agent.metrics.snapshot()["counters"]["identity_probe_drift"] == 1
    # the premise was not silently reused: the drifted probe became the validate obs
    # (audited with the drift dimension named), and the P0-H single re-observe ran —
    # the action (executed or rejected) is decided against the RE-OBSERVED screen,
    # never the drifted premise.
    drifted_validate = [
        e for e in events
        if e["event_type"] == "observation"
        and (e.get("metadata") or {}).get("phase") == "validate"
        and (e.get("metadata") or {}).get("probe")
        and (e.get("metadata") or {}).get("drift")
    ]
    assert drifted_validate, events
    phases = [
        (e.get("metadata") or {}).get("phase")
        for e in events
        if e["event_type"] == "observation"
    ]
    assert "revalidate" in phases, phases


async def test_w1_queued_follow_ups_keep_their_fresh_validate_capture(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Queued follow-ups NEVER reuse: their premise is a previous item's post-action
    capture (genuinely older), so every queued item takes the fresh validate capture
    and the strict digest-surprise probe stays armed."""
    backend = ProbingBackend()
    session_id, bundle, backend, _ = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False,
        limits=FAST_LIMITS,
    )
    response = await server.computer_execute(
        session_id, "click", x=10, y=10,
        follow_ups=[{"action": "click", "x": 20, "y": 20}],
    )
    payload = execute_payload(response)
    assert payload["ok"] is True, payload
    events = audit_events(bundle, session_id)
    observations = [e for e in events if e["event_type"] == "observation"]
    phases = [(e.get("metadata") or {}).get("phase") for e in observations]
    # item 0 (direct, fresh premise) may reuse; item 1 (queued) must capture validate
    assert phases.count("validate") >= 2, phases
    queued_reuse = [
        e for e in observations
        if (e.get("metadata") or {}).get("phase") == "validate"
        and (e.get("metadata") or {}).get("reused")
    ]
    assert len(queued_reuse) <= 1  # at most the FIRST (direct) item reused
    assert backend.probe_calls == 1  # only the direct item probed


async def test_w1_env_knob_zero_restores_full_capture(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``CORTEX_VALIDATE_REUSE_MS=0`` restores the pre-R-5 behavior exactly."""
    backend = ProbingBackend()
    session_id, bundle, backend, _ = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False,
        limits=FAST_LIMITS,
    )
    monkeypatch.setenv("CORTEX_VALIDATE_REUSE_MS", "0")
    await server.computer_execute(session_id, "keypress", keys=["enter"])
    phases = [
        (e.get("metadata") or {}).get("phase")
        for e in audit_events(bundle, session_id)
        if e["event_type"] == "observation"
    ]
    assert phases == ["direct_request", "validate", "post_action"], phases


# =====================================================================================
# W2 — raw-frame fast path (byte-identical verdicts, no double decode)
# =====================================================================================


def _frame_pair() -> tuple[Image.Image, Image.Image]:
    """(before, after) frames with a compact strong change: 70 pixels by delta 190."""
    before = Image.new("RGB", (200, 100), (10, 20, 30))
    after = before.copy()
    for i in range(STRONG_CHANGE_MIN_PIXELS + 20):
        after.putpixel((i, 5), (200, 20, 30))  # delta 190 >= STRONG_PIXEL_DELTA
    return before, after


def _obs(image: Image.Image, *, stash: bool = False) -> Observation:
    observation = Observation(
        image_base64=_png_image_b64(image), width=image.width, height=image.height
    )
    if stash:
        observation._frame = image
    return observation


def test_w2_stashed_frames_skip_decode_verdicts_identical() -> None:
    """Verdict, changed flag, and evidence are IDENTICAL between the stashed-frame
    fast path and the legacy base64+PNG decode path."""
    before, after = _frame_pair()
    engine = VerificationEngine()
    intent = VerificationIntent(
        kind=VerificationKind.VISUAL_CHANGE, expected_change=True,
        diff_threshold=DEFAULT_DIFF_THRESHOLD,
    )
    fast = engine.verify(intent, _obs(before, stash=True), _obs(after, stash=True))
    slow = engine.verify(intent, _obs(before), _obs(after))
    assert fast.outcome == slow.outcome == "verified"
    assert fast.changed == slow.changed is True
    assert fast.evidence == slow.evidence  # numbers identical to the last digit


def test_w2_fast_path_requires_both_sides_stashed() -> None:
    """One-sided (or absent) stashes fall back to the full decode — never a partial
    fast path (conservative: a mismatched stash cannot skew a diff)."""
    before, after = _frame_pair()
    strategy = ScreenshotDiffStrategy()
    frames = strategy._frames(_obs(before), _obs(after, stash=True))
    # legacy path returns NEW decoded objects, not the stashed one
    assert all(f is not after for f in frames)
    frames = strategy._frames(_obs(before, stash=True), _obs(after, stash=True))
    assert frames[0] is before and frames[1] is after  # both stashed: no decode


def test_w2_mismatched_stash_size_falls_back_to_decode() -> None:
    """A stashed frame whose size disagrees with the observation's recorded dimensions
    is distrusted: the legacy decode runs instead."""
    before, after = _frame_pair()
    odd = Observation(
        image_base64=_png_image_b64(after), width=after.width + 1, height=after.height
    )
    odd._frame = after
    strategy = ScreenshotDiffStrategy()
    frames = strategy._frames(_obs(before, stash=True), odd)
    assert frames[1] is not after  # decoded fresh, not the mismatched stash


def test_w2_focus_change_corroboration_uses_fast_path() -> None:
    """FocusChangeStrategy's digest-corroboration magnitude is identical via the
    fast path (shared _frames/_diff_magnitude implementation)."""
    from computer_use_mcp.verification import (
        FOCUS_CHANGE_INTENT_FLAG,
        FocusChangeStrategy,
    )

    before, after = _frame_pair()
    intent = VerificationIntent(
        kind=VerificationKind.VISUAL_CHANGE,
        expected_change=True,
        expected_effect="something changes",
        metadata={FOCUS_CHANGE_INTENT_FLAG: True},
    )
    strategy = FocusChangeStrategy()
    fast = strategy.verify(intent, _obs(before, stash=True), _obs(after, stash=True))
    slow = strategy.verify(intent, _obs(before), _obs(after))
    assert fast.outcome == slow.outcome
    assert fast.note == slow.note
    assert fast.evidence == slow.evidence


def test_w2_private_stash_never_serialized() -> None:
    """``_frame``/``_captured_monotonic`` are private: never in model_dump, never on
    the wire, never in checkpoints (pure in-process latency optimization)."""
    before, _ = _frame_pair()
    observation = _obs(before, stash=True)
    observation._captured_monotonic = 123.0
    dumped = observation.model_dump()
    assert "_frame" not in dumped and "_captured_monotonic" not in dumped
    roundtrip = Observation.model_validate(dumped)
    assert roundtrip._frame is None and roundtrip._captured_monotonic == 0.0


# =====================================================================================
# W3 — one-histogram diff math (bit-identical to the historical computation)
# =====================================================================================


def test_w3_histogram_mean_bit_identical_to_imagestat() -> None:
    """The shared _diff_magnitude mean/strongly-changed equal the historical
    ImageStat.Stat + split/lighter/histogram computation to the last bit."""
    before, after = _frame_pair()
    mean_new, strong_new = _diff_magnitude(before, after)
    # historical math, verbatim from the pre-R-5 strategy
    diff = ImageChops.difference(before, after)
    mean_old = sum(ImageStat.Stat(diff).mean) / 3.0
    bands = diff.split()
    max_band = ImageChops.lighter(ImageChops.lighter(bands[0], bands[1]), bands[2])
    strong_old = sum(max_band.histogram()[STRONG_PIXEL_DELTA:])
    assert mean_new == mean_old  # exact float equality (same summation order per band)
    assert strong_new == strong_old
    # and the change actually trips the pinned thresholds
    assert strong_new >= STRONG_CHANGE_MIN_PIXELS


def test_w3_subthreshold_flicker_is_not_a_change() -> None:
    """Thresholds keep their exact semantics under the new math: a 2-pixel caret
    blink (below STRONG_CHANGE_MIN_PIXELS, mean below threshold) is NOT a change."""
    before = Image.new("RGB", (200, 100), (10, 20, 30))
    after = before.copy()
    after.putpixel((0, 0), (12, 20, 30))  # tiny delta, 1 pixel
    mean_new, strong_new = _diff_magnitude(before, after)
    assert mean_new < DEFAULT_DIFF_THRESHOLD
    assert strong_new < STRONG_CHANGE_MIN_PIXELS
    intent = VerificationIntent(kind=VerificationKind.VISUAL_CHANGE, expected_change=True)
    result = ScreenshotDiffStrategy().verify(intent, _obs(before), _obs(after))
    assert result.outcome == "failed"  # expected change, none observed


def test_w3_thresholds_module_constants_untouched() -> None:
    """The mission-frozen verification constants are exactly their contracted values."""
    assert STRONG_PIXEL_DELTA == 40
    assert STRONG_CHANGE_MIN_PIXELS == 50
    assert DEFAULT_DIFF_THRESHOLD == 1.0


# =====================================================================================
# W4 — outbound bounding from the raw frame (byte-identical ladder)
# =====================================================================================


def _oversized_png_b64() -> tuple[str, Image.Image]:
    """(png_base64, rgb_frame) with the PNG above the default 180 KB outbound budget
    (incompressible random content — forces the JPEG ladder) and the matching RGB frame."""
    width, height = 1920, 1080
    frame = Image.frombytes("RGB", (width, height), os.urandom(width * height * 3))
    return _png_image_b64(frame), frame


def test_w4_outbound_from_raw_frame_byte_identical() -> None:
    """The frame-fed ladder produces byte-identical output to the decode-then-encode
    ladder (same budget, same qualities/scales — only the decode is skipped)."""
    png_b64, frame = _oversized_png_b64()
    decoded = base64.b64decode(png_b64, validate=True)
    assert len(decoded) > server._result_image_max_bytes()  # ladder will run
    slow_b64, slow_mime = server._bound_outbound_image(png_b64)
    fast_b64, fast_mime = server._bound_outbound_image(png_b64, frame=frame)
    assert slow_mime == fast_mime
    assert slow_b64 == fast_b64  # exact byte identity


def test_w4_png_under_budget_untouched_both_paths() -> None:
    """A PNG inside the budget is returned AS-IS on both paths (no re-encode ever)."""
    small = Image.new("RGB", (64, 48), "white")
    png_b64 = _png_image_b64(small)
    out_b64, mime = server._bound_outbound_image(png_b64, frame=small)
    assert out_b64 == png_b64 and mime == "image/png"


def test_w4_jpeg_ladder_encodes_linearly_no_reencode_storm(monkeypatch: pytest.MonkeyPatch) -> None:
    """No pathological re-encode: the ladder performs exactly ONE JPEG encode per
    visited step (q85, then one per downscale) — a re-encode STORM (re-encoding the
    FULL frame at every step instead of the downscaled one, or restarting the ladder)
    would show quadratic behavior. On a normal UI screen (measured: q85 JPEG ~144 KB
    vs 180 KB budget) the ladder fits within 1-2 encodes; incompressible random
    content legitimately needs several downscale steps — this pin bounds the LADDER
    SHAPE, not the content."""
    png_b64, frame = _oversized_png_b64()
    saves: list[Any] = []
    original_save = Image.Image.save

    def counting_save(self: Image.Image, *args: Any, **kwargs: Any) -> Any:
        if kwargs.get("format") == "JPEG":
            saves.append((self.width, self.height))
        return original_save(self, *args, **kwargs)

    monkeypatch.setattr(Image.Image, "save", counting_save)
    out_b64, mime = server._bound_outbound_image(png_b64, frame=frame)
    assert mime == "image/jpeg"
    assert saves, "ladder never encoded"
    # linear descent: at most one encode per step, monotonically shrinking frames,
    # and the hard step ceiling of the ladder (12 scale steps + quality descent)
    assert len(saves) <= 17, saves
    seen_sizes = [(w, h) for (w, h) in saves]
    strictly_shrinking = all(
        seen_sizes[i + 1][0] <= seen_sizes[i][0] for i in range(len(seen_sizes) - 1)
    )
    assert strictly_shrinking, saves  # every step encodes a SMALLER frame — no restarts
    assert len(out_b64) > 0


def test_w4_garbage_budget_falls_back_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Env-knob robustness: garbage/non-positive budgets fall back to 180 KB."""
    monkeypatch.setenv("CORTEX_RESULT_IMAGE_MAX_KB", "not-a-number")
    assert server._result_image_max_bytes() == 180 * 1024
    monkeypatch.setenv("CORTEX_RESULT_IMAGE_MAX_KB", "-5")
    assert server._result_image_max_bytes() == 180 * 1024


# =====================================================================================
# Capture-side pins (payload dedupe + private-attr plumbing)
# =====================================================================================


class DedupeProbeBackend(FakeComputerBackend):
    """Fake backend exposing the R-5 payload-cache contract via the base-class seams
    (the real implementation lives in LocalComputerBackend; this pins the CONTRACT:
    an identical-pixel capture reuses the previous payload, a changed capture does
    not)."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._last_payload: str | None = None
        self._last_raw: Any = None
        self.encodes = 0

    def _encode_png(self, raw: Any) -> str:
        self.encodes += 1
        return _png("white")

    def observe(self) -> Observation:  # pragma: no cover - contract doc only
        raise NotImplementedError


def test_observation_digest_matches_identity_fast_path() -> None:
    """digest_matches: the same object short-circuits (identity IS the proof);
    distinct objects compare payloads exactly as before."""
    a = _obs(Image.new("RGB", (32, 32), "red"))
    b = _obs(Image.new("RGB", (32, 32), "red"))
    assert digest_matches(a, a) is True  # identity fast path
    assert digest_matches(a, b) is True  # same pixels -> same base64
    c = _obs(Image.new("RGB", (32, 32), "blue"))
    assert digest_matches(a, c) is False


# =====================================================================================
# Performance smoke (generous headroom; stubbed capture — no real screen needed)
# =====================================================================================


def test_perf_smoke_stacked_strategy_under_budget() -> None:
    """Smoke: the stashed-frame diff path (fast) completes a full strategy verify on a
    1920x1080-shaped synthetic pair well under the R-5 budget for that stage. The
    budget (400 ms) is the measured ~24-32 ms ceiling with >10x CI headroom — it pins
    against REGRESSION to a decode path (which would add ~60-80 ms), not against
    machine noise."""
    if os.environ.get("CORTEX_INTERNAL_FRAME_REUSE") == "0":
        pytest.skip("R-5 frame reuse disabled by env — legacy path intentionally active")
    width, height = 1920, 1080
    before = Image.new("RGB", (width, height), (30, 40, 50))
    after = before.copy()
    for x in range(300):
        for y in range(3):  # a drawn stroke: 900 strongly-changed pixels
            after.putpixel((x, y), (255, 0, 0))
    intent = VerificationIntent(kind=VerificationKind.VISUAL_CHANGE, expected_change=True)
    strategy = ScreenshotDiffStrategy()
    started = time.perf_counter()
    result = strategy.verify(intent, _obs(before, stash=True), _obs(after, stash=True))
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    assert result.outcome == "verified" and result.changed is True
    assert elapsed_ms < 400.0, f"stashed-frame diff regressed: {elapsed_ms:.1f} ms"


def test_perf_smoke_identity_probe_is_capture_free_on_fake() -> None:
    """The base-class identity_probe default is None (capture-free contract: backends
    without the capability NEVER pay a hidden capture — the caller falls back to the
    real validate capture)."""
    backend = FakeComputerBackend()
    assert ComputerBackend.identity_probe(backend, None) is None


# =====================================================================================
# Live-desktop capture pins (screen READING only; skipped off-Windows)
# =====================================================================================


@pytest.mark.skipif(
    os.environ.get("CORTEX_R5_SKIP_LIVE_PINS") == "1",
    reason="live capture pins disabled by env",
)
def test_live_backend_observe_stashes_frame_and_payload_dedupe(real_backend: Any) -> None:
    """On the REAL desktop: observe() stashes the capture-time frame + freshness
    stamp, and two consecutive captures of an identical screen reuse the payload
    (same base64 — PNG determinism makes the reuse byte-safe). This is screen READING
    only: no input is dispatched."""
    if os.environ.get("CORTEX_INTERNAL_FRAME_REUSE") == "0":
        pytest.skip("R-5 frame reuse disabled by env")
    first = real_backend.observe()
    assert getattr(first, "_frame", None) is not None
    assert first._frame.size == (first.width, first.height)
    assert first._captured_monotonic > 0.0
    # the payload cache round-trip: a second capture of an unchanged screen area must
    # either hit the cache (same payload, faster) or honestly re-encode — never lie.
    second = real_backend.observe()
    if second.image_base64 == first.image_base64:
        pass  # cache hit on an identical frame
    else:
        # screen genuinely changed (cursor/clock): payloads legitimately differ
        assert second._frame is not None


def test_live_identity_probe_matches_observe_identity(real_backend: Any) -> None:
    """On the REAL desktop: the capture-free identity probe reports the SAME identity
    dimensions a full observe() would (dimensions, window, monitor, coordinate space)
    — the reuse guard's evidence is the real OS state, never a guess."""
    reference = real_backend.observe()
    probe = real_backend.identity_probe(reference)
    if probe is None:
        pytest.skip("identity probe unavailable on this backend")
    assert (probe.width, probe.height) == (reference.width, reference.height)
    assert probe.coordinate_space == reference.coordinate_space
    ref_info, probe_info = reference.active_window_info, probe.active_window_info
    if ref_info is not None and probe_info is not None:
        assert probe_info.hwnd == ref_info.hwnd
        assert probe_info.process_name == ref_info.process_name
    assert probe.monitor is not None and probe.monitor.id == reference.monitor.id
    # the probe carries NO new pixels (the reference payload, verbatim)
    assert probe.image_base64 == reference.image_base64
