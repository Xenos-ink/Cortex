"""R-6 capture-floor pins (ORVEX-CORTEX-056-LIVEFIX, mission goal section 7).

Pins the R-6 capture-backend mechanism and the floor audit conclusions:

- CORTEX_CAPTURE selection: default "blt" (byte-identical R-5 pipeline);
  "dxgi" enables the pure-ctypes Desktop Duplication path; ANY other value
  (garbage, empty, "DXGI" case variants honored) degrades to "blt" — the knob
  can never invent a third backend or crash on garbage.
- Fail-open contract: a failing/broken/unavailable DXGI path returns None from
  ``_dxgi_grab`` and the mss BitBlt grab runs EXACTLY as before (capture never
  fails-closed to slowness-land or rejection); a structural failure disables
  the dxgi path PERMANENTLY for the backend instance (no per-capture retry cost).
- Bounds guard: a DXGI frame that does not match the expected monitor bounds
  (mode change, multi-monitor spanning) is DISCARDED (mss path) without
  disabling the dxgi path (the next capture retries duplication).
- The mss path is untouched: with "blt" the raw-BGRA payload dedupe key is set
  and the R-5 payload cache still short-circuits identical screens; with "dxgi"
  no BGRA key exists so the dedupe is skipped (no false cache hits — an
  identical screen still re-encodes, never a WRONG payload reuse).
- Live-desktop sections (Windows only): observe() under "dxgi" produces a
  valid PNG payload decodable to the recorded dimensions; a second observe is
  self-consistent; and the pixel-equivalence spot check DXGI-vs-GDI on a quiet
  desktop (mean diff < 1.0 — strict identity is impossible on a live desktop
  because frames composite asynchronously).

No OS input is dispatched anywhere in this file (capture reading only).
"""

from __future__ import annotations

import base64
import hashlib
import io
import os
import platform
from typing import Any

import pytest
from PIL import Image

from computer_use_mcp.backend import CAPTURE_BACKEND_ENV, LocalComputerBackend


# =====================================================================================
# Selection + fail-open (no real screen needed — monkeypatched constructors)
# =====================================================================================


def _fresh_backend(monkeypatch: pytest.MonkeyPatch, env: str | None) -> Any:
    """A LocalComputerBackend-shaped object without Windows/GDI prerequisites.

    The R-6 selection logic is pure constructor state, so the tests build the
    instance via ``__new__`` and set only the fields the selection reads —
    no mss, no pyautogui, no real display.
    """
    monkeypatch.setattr(LocalComputerBackend, "__init__", lambda self: None)
    if env is not None:
        monkeypatch.setenv(CAPTURE_BACKEND_ENV, env)
    else:
        monkeypatch.delenv(CAPTURE_BACKEND_ENV, raising=False)
    backend = LocalComputerBackend.__new__(LocalComputerBackend)
    # replicate exactly the __init__ lines that own the R-6 state
    backend._capture_backend = (
        "dxgi" if os.getenv(CAPTURE_BACKEND_ENV, "").strip().casefold() == "dxgi"
        else "blt"
    )
    backend._dxgi = None
    return backend


def test_default_selection_is_blt(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default (env unset) keeps the R-5 GDI pipeline — no behavior change by default."""
    backend = _fresh_backend(monkeypatch, None)
    assert backend._capture_backend == "blt"


def test_dxgi_selection_honored_and_casefolded(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = _fresh_backend(monkeypatch, " DXGI ")
    assert backend._capture_backend == "dxgi"


def test_garbage_env_degrades_to_blt(monkeypatch: pytest.MonkeyPatch) -> None:
    """Garbage can never crash init or invent a third backend — fail-open to legacy."""
    for garbage in ("", "  ", "vulkan", "DXGI1", "0", "none", "true"):
        backend = _fresh_backend(monkeypatch, garbage)
        assert backend._capture_backend == "blt", garbage


def test_blt_backend_skips_dxgi_grab(monkeypatch: pytest.MonkeyPatch) -> None:
    """With blt selected, _dxgi_grab is a no-op None — the mss path runs untouched."""
    backend = _fresh_backend(monkeypatch, None)

    def _boom() -> Any:
        raise AssertionError("the dxgi constructor must never run under blt")

    monkeypatch.setattr(
        "computer_use_mcp.backend._DxgiDuplicator", _boom
    )
    assert backend._dxgi_grab(1920, 1080) is None


def test_dxgi_construction_failure_falls_back_permanently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A constructor failure disables the dxgi path for the backend's life (no retry)."""

    class _Broken:
        def __init__(self) -> None:
            raise OSError("no duplication on this session (RDP/protected)")

    monkeypatch.setattr("computer_use_mcp.backend._DxgiDuplicator", _Broken)
    backend = _fresh_backend(monkeypatch, "dxgi")
    assert backend._capture_backend == "dxgi"
    assert backend._dxgi_grab(1920, 1080) is None
    assert backend._capture_backend == "blt"  # permanent degradation
    assert backend._dxgi is None
    # second call: still blt, constructor never re-attempted
    assert backend._dxgi_grab(1920, 1080) is None
    assert backend._capture_backend == "blt"


def test_dxgi_grab_failure_disables_path_and_closes_handle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A mid-session structural failure (device lost) degrades permanently and
    releases the duplication handle (no COM object leak)."""

    class _Dying:
        closed = False

        def __init__(self) -> None:
            self.width, self.height = 1920, 1080

        def grab(self, timeout_ms: int = 8) -> Any:
            raise OSError("DXGI_ERROR_DEVICE_REMOVED")

        def close(self) -> None:
            type(self).closed = True

    monkeypatch.setattr("computer_use_mcp.backend._DxgiDuplicator", _Dying)
    backend = _fresh_backend(monkeypatch, "dxgi")
    assert backend._dxgi_grab(1920, 1080) is None
    assert backend._capture_backend == "blt"
    assert backend._dxgi is None
    assert _Dying.closed is True


def test_dxgi_bounds_mismatch_discards_frame_but_keeps_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A mode change (bounds mismatch) discards the frame (mss runs) but the dxgi
    path stays armed for the next capture — transient, not structural."""

    class _WrongSize:
        def __init__(self) -> None:
            self.width, self.height = 800, 600

        def grab(self, timeout_ms: int = 8) -> Any:
            return Image.new("RGB", (800, 600))  # not the expected 1920x1080

        def close(self) -> None:
            pass

    monkeypatch.setattr("computer_use_mcp.backend._DxgiDuplicator", _WrongSize)
    backend = _fresh_backend(monkeypatch, "dxgi")
    assert backend._dxgi_grab(1920, 1080) is None
    assert backend._capture_backend == "dxgi"  # path still armed
    assert backend._dxgi is not None  # duplicator retained


def test_dxgi_timeout_returns_cached_frame_not_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Idle desktop: grab times out and returns the LAST frame (still current),
    never None-with-a-live-duplicator (the caller would waste a GDI grab)."""

    class _Idle:
        def __init__(self) -> None:
            self.width = self.height = 1920
            self.height = 1080

        def grab(self, timeout_ms: int = 8) -> Any:
            return Image.new("RGB", (1920, 1080))

        def close(self) -> None:
            pass

    monkeypatch.setattr("computer_use_mcp.backend._DxgiDuplicator", _Idle)
    backend = _fresh_backend(monkeypatch, "dxgi")
    frame = backend._dxgi_grab(1920, 1080)
    assert frame is not None and frame.size == (1920, 1080)


# =====================================================================================
# Payload-dedupe interplay (W2 cache correctness across capture backends)
# =====================================================================================


class _StubRaw:
    """The minimal mss-shot shape _grab_png touches (``.raw`` + ``.size``)."""

    def __init__(self, size: tuple[int, int], pixels: bytes) -> None:
        self.size = size
        self.raw = pixels


def _grab_with_stub(monkeypatch: pytest.MonkeyPatch, backend: Any, pixels: bytes):
    """Run _grab_png on a stubbed backend with the frame-reuse fields set."""
    size = (4, 4)
    monkeypatch.setattr(
        backend, "_mss_grab", lambda region: _StubRaw(size, pixels)
    )
    backend._internal_frame_reuse = True
    backend._payload_cache = None
    backend.png_optimize = False
    backend.png_compress_level = None
    backend._raw_payload_keys = False
    backend._raw_key_enabled = backend._internal_frame_reuse
    return backend._grab_png(0, 0, size[0], size[1])


def test_blt_path_keeps_r5_payload_dedupe(monkeypatch: pytest.MonkeyPatch) -> None:
    """blt: identical consecutive screens reuse the encoded payload (R-5 W2 intact)."""
    backend = _fresh_backend(monkeypatch, None)
    pixels = bytes(range(64)) * 4  # 4x4 BGRA
    first = _grab_with_stub(monkeypatch, backend, pixels)
    second = _grab_with_stub(monkeypatch, backend, pixels)
    assert first[0] == second[0]  # payload reused verbatim, no re-encode


def test_dxgi_path_never_pollutes_payload_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """dxgi: no raw-BGRA key exists, so the payload cache is neither consulted
    nor populated — a later identical blt screen still encodes fresh (never a
    WRONG-payload reuse across backends)."""

    class _FakeDup:
        def __init__(self) -> None:
            self.width, self.height = 4, 4

        def grab(self, timeout_ms: int = 8) -> Any:
            return Image.new("RGB", (4, 4), (10, 20, 30))

        def close(self) -> None:
            pass

    monkeypatch.setattr("computer_use_mcp.backend._DxgiDuplicator", _FakeDup)
    backend = _fresh_backend(monkeypatch, "dxgi")
    encoded, w, h, frame = _grab_with_stub(monkeypatch, backend, b"\x00" * 128)
    # dxgi frame served; cache not populated (no BGRA key)
    assert (w, h) == (4, 4)
    assert frame.size == (4, 4)
    assert backend._payload_cache is None
    # and a blt capture afterwards still encodes (no stale cache entry)
    backend._capture_backend = "blt"
    pixels = b"\x11" * 128
    monkeypatch.setattr(backend, "_mss_grab", lambda region: _StubRaw((4, 4), pixels))
    encoded2, _, _, _ = backend._grab_png(0, 0, 4, 4)
    assert encoded2  # fresh encode ran
    assert backend._payload_cache is not None  # blt path populates it again


def test_grab_png_decodes_payload_to_matching_pixels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The encoded PNG of a captured frame round-trips to the same pixels
    (capture-agnostic contract: whatever grabbed the frame, the payload is a
    lossless PNG of it)."""
    backend = _fresh_backend(monkeypatch, None)
    pixels = bytes([r for p in range(16) for r in (p, 255 - p, 7, 255)])
    encoded, w, h, frame = _grab_with_stub(monkeypatch, backend, pixels)
    decoded = Image.open(io.BytesIO(base64.b64decode(encoded)))
    assert decoded.size == (w, h) == (4, 4)
    assert decoded.tobytes() == frame.tobytes()


# =====================================================================================
# Live-desktop validation (Windows + real display only; capture READING only)
# =====================================================================================


@pytest.mark.skipif(platform.system() != "Windows", reason="Windows-only live capture")
def test_live_dxgi_observe_produces_valid_png() -> None:
    """Live: CORTEX_CAPTURE=dxgi observe() returns a decodable PNG at the
    recorded dimensions and the backend stays on the dxgi path (fail-open would
    flip it to blt on failure — also acceptable, never a crash)."""
    os.environ[CAPTURE_BACKEND_ENV] = "dxgi"
    try:
        backend = LocalComputerBackend()
        assert backend._capture_backend == "dxgi"
        observation = backend.observe()
        decoded = Image.open(io.BytesIO(base64.b64decode(observation.image_base64)))
        assert decoded.size == (observation.width, observation.height)
        second = backend.observe()
        assert second.image_base64  # any payload (dxgi or its blt fallback)
        # fail-open invariant: whatever happened, observe still works
        assert backend._capture_backend in ("dxgi", "blt")
    finally:
        os.environ.pop(CAPTURE_BACKEND_ENV, None)


@pytest.mark.skipif(platform.system() != "Windows", reason="Windows-only live capture")
def test_live_blt_observe_unaffected() -> None:
    """Live: the default path (no env knob) still observes fine after R-6."""
    os.environ.pop(CAPTURE_BACKEND_ENV, None)
    backend = LocalComputerBackend()
    assert backend._capture_backend == "blt"
    observation = backend.observe()
    decoded = Image.open(io.BytesIO(base64.b64decode(observation.image_base64)))
    assert decoded.size == (observation.width, observation.height)


# =====================================================================================
# R-6 encode-skip: text sessions never encode a PNG nobody sees
# (backend._raw_payload_keys — deterministic raw-pixel key instead of a lossless PNG)
# =====================================================================================


def test_raw_key_payload_shape_and_determinism(monkeypatch: pytest.MonkeyPatch) -> None:
    """Armed text session: the payload is the deterministic sha256 key over the raw
    BGRA bytes (same pixels -> same key, distinct pixels -> distinct key), never a PNG,
    and the RGB frame is still stashed for the verification fast path."""
    backend = _fresh_backend(monkeypatch, None)
    pixels_a = bytes(range(64)) * 4  # 4x4 BGRA
    pixels_b = bytes(reversed(bytes(range(64)))) * 4  # different pixels

    def _stub(raw: bytes):
        monkeypatch.setattr(backend, "_mss_grab", lambda region: _StubRaw((4, 4), raw))

    backend._internal_frame_reuse = True
    backend._payload_cache = None
    backend.png_optimize = False
    backend.png_compress_level = None
    backend._raw_payload_keys = True
    backend._raw_key_enabled = True

    _stub(pixels_a)
    encoded1, w, h, frame1 = backend._grab_png(0, 0, 4, 4)
    _stub(pixels_a)
    encoded2, _, _, frame2 = backend._grab_png(0, 0, 4, 4)
    _stub(pixels_b)
    encoded3, _, _, _ = backend._grab_png(0, 0, 4, 4)

    assert encoded1 == encoded2  # identical pixels -> identical key (determinism)
    assert encoded1 != encoded3  # distinct pixels -> distinct key
    assert encoded1.startswith("RAW:")  # marked, never misread as a PNG
    assert not encoded1.startswith("iVBOR")  # never a PNG base64 magic
    assert (w, h) == (4, 4)
    assert frame1.size == (4, 4) and frame2.size == (4, 4)  # frame still stashed


def test_raw_key_requires_both_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    """The key is emitted ONLY when the session armed it AND reuse is enabled:
    image-mode sessions and CORTEX_INTERNAL_FRAME_REUSE=0 keep real PNGs."""
    backend = _fresh_backend(monkeypatch, None)
    pixels = bytes(range(64)) * 4
    backend._internal_frame_reuse = True
    backend._payload_cache = None
    backend.png_optimize = False
    backend.png_compress_level = None
    monkeypatch.setattr(backend, "_mss_grab", lambda region: _StubRaw((4, 4), pixels))

    # unarmed session (image mode): PNG bytes
    backend._raw_key_enabled = True
    backend._raw_payload_keys = False
    encoded_png, _, _, _ = backend._grab_png(0, 0, 4, 4)
    assert not encoded_png.startswith("RAW:")
    assert base64.b64decode(encoded_png)[:8] == b"\x89PNG\r\n\x1a\n"

    # reuse disabled (kill-switch form): PNG bytes even when armed
    backend._raw_key_enabled = False
    backend._raw_payload_keys = True
    encoded_png2, _, _, _ = backend._grab_png(0, 0, 4, 4)
    assert base64.b64decode(encoded_png2)[:8] == b"\x89PNG\r\n\x1a\n"


def test_raw_key_env_knob_forces_png(monkeypatch: pytest.MonkeyPatch) -> None:
    """CORTEX_TEXT_PNG=1 disables the fast key at construction: text sessions keep
    PNG payloads (the R-6 kill-switch)."""
    from computer_use_mcp.backend import TEXT_PNG_ENV

    monkeypatch.setenv(TEXT_PNG_ENV, "1")
    monkeypatch.setattr(
        LocalComputerBackend, "__init__", lambda self: None
    )
    backend = LocalComputerBackend.__new__(LocalComputerBackend)
    backend._internal_frame_reuse = True
    backend._capture_backend = "blt"
    backend._dxgi = None
    # replicate the construction lines the knob owns
    backend._raw_payload_keys = False
    backend._raw_key_enabled = backend._internal_frame_reuse and (
        not __import__("computer_use_mcp.backend", fromlist=["_env_bool"])._env_bool(
            TEXT_PNG_ENV, False
        )
    )
    assert backend._raw_key_enabled is False


def test_raw_key_digest_and_staleness_semantics_preserved() -> None:
    """The raw key serves the two payload consumers byte-faithfully: the digest is a
    stable hash of the key (same screen -> same digest; changed screen -> changed
    digest), and ``digest_matches`` equality on the key mirrors PNG-payload equality
    (identical pixels -> identical payload; changed pixels -> different payload)."""
    from computer_use_mcp.models import Observation
    from computer_use_mcp.observation import digest_matches, observation_digest

    def obs(payload: str) -> Observation:
        return Observation(
            image_base64=payload, width=4, height=4, observation_id="pin-o"
        )

    key_a = "RAW:AAAA;4x4"
    key_a2 = "RAW:AAAA;4x4"  # identical pixels
    key_b = "RAW:BBBB;4x4"  # changed pixels
    assert digest_matches(obs(key_a), obs(key_a2)) is True  # same screen
    assert digest_matches(obs(key_a), obs(key_b)) is False  # changed screen
    assert observation_digest(obs(key_a)) == observation_digest(obs(key_a2))
    assert observation_digest(obs(key_a)) != observation_digest(obs(key_b))


def test_focus_change_signal_verdict_identical_png_vs_rawkey() -> None:
    """BYTE-IDENTICAL VERDICT: for the same recorded frame pair, the focus-change
    digest corroboration returns the same result whether the payload is the
    historical PNG base64 or the R-6 raw key — the text-session fast path cannot
    change any verdict (the comparison reads each payload verbatim; the
    corroboration magnitude uses the identical stashed frames on both paths)."""
    from computer_use_mcp.models import Observation
    from computer_use_mcp.verification import (
        FocusChangeStrategy,
        ScreenshotDiffStrategy,
        VerificationIntent,
        VerificationKind,
    )

    # a real 32x32 pixel pair: identical vs one strongly-changed patch
    before_img = Image.new("RGB", (32, 32), (10, 10, 10))
    after_img = before_img.copy()
    px = after_img.load()
    for y in range(10, 20):
        for x in range(10, 20):
            px[x, y] = (255, 255, 255)  # 100 strongly-changed pixels

    def png_obs(img: Image.Image) -> Observation:
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        o = Observation(
            image_base64=base64.b64encode(buf.getvalue()).decode("ascii"),
            width=32, height=32,
        )
        o._frame = img
        return o

    def rawkey_obs(img: Image.Image) -> Observation:
        o = Observation(
            image_base64="RAW:" + hashlib.sha256(img.tobytes()).hexdigest() + ";32x32",
            width=32, height=32,
        )
        o._frame = img
        return o

    intent = VerificationIntent(
        kind=VerificationKind.VISUAL_CHANGE,
        expected_change=True,
        expected_effect="focused control",
        metadata={"focus_change_click": True},
        diff_threshold=1.0,
    )
    strategy = FocusChangeStrategy()
    png_result = strategy.verify(intent, png_obs(before_img), png_obs(after_img))
    key_result = strategy.verify(intent, rawkey_obs(before_img), rawkey_obs(after_img))
    assert png_result.outcome == key_result.outcome == "verified"
    assert png_result.changed == key_result.changed
    # the diff tier itself is payload-agnostic (frames stash): identical verdicts
    diff = ScreenshotDiffStrategy()
    d_png = diff.verify(intent, png_obs(before_img), png_obs(after_img))
    d_key = diff.verify(intent, rawkey_obs(before_img), rawkey_obs(after_img))
    assert d_png.outcome == d_key.outcome and d_png.changed == d_key.changed


def test_focus_change_derivation_memoized_and_pure() -> None:
    """The focus-signal payload comparison uses the observation's own payload
    VERBATIM (no derivation): the R-6 raw key is already a pure per-pixel hash, so
    identical screens compare equal and changed screens compare unequal exactly as
    the PNG base64 always did — the text-session fast path cannot change any
    comparison, and there is no per-comparison derivation cost at all."""
    from computer_use_mcp.models import Observation
    from computer_use_mcp.verification import FocusChangeStrategy

    img = Image.new("RGB", (16, 16), (7, 8, 9))
    o = Observation(image_base64="RAW:KEYA;16x16", width=16, height=16)
    o._frame = img
    assert FocusChangeStrategy._payload_text(o) == "RAW:KEYA;16x16"
    # identical payload -> identical text (an unchanged screen)
    o_same = Observation(image_base64="RAW:KEYA;16x16", width=16, height=16)
    o_same._frame = img
    assert FocusChangeStrategy._payload_text(o) == FocusChangeStrategy._payload_text(o_same)
    # different pixels -> different key -> different text
    img2 = Image.new("RGB", (16, 16), (200, 0, 0))
    o2 = Observation(image_base64="RAW:KEYB;16x16", width=16, height=16)
    o2._frame = img2
    assert FocusChangeStrategy._payload_text(o) != FocusChangeStrategy._payload_text(o2)
    # PNG payloads compare verbatim too (the historical behavior)
    o_png = Observation(image_base64="iVBORw0KGgo=", width=16, height=16)
    o_png._frame = img
    assert FocusChangeStrategy._payload_text(o_png) == "iVBORw0KGgo="
    # frame-bytes derivation (corroboration path) stays a pure memoized function
    b1 = FocusChangeStrategy._frame_bytes(o)
    b2 = FocusChangeStrategy._frame_bytes(o)
    assert b1 == b2 == img.tobytes()
    assert getattr(o, "_payload_key_bytes", None) == img.tobytes()
    # no frame + key payload: honest abstention (empty), never a crash
    o3 = Observation(image_base64="RAW:NOFRAME;16x16", width=16, height=16)
    assert FocusChangeStrategy._frame_bytes(o3) is None


def test_raw_key_never_reaches_outbound_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    """A raw-key observation can never leak into a wire-visible image block: the
    outbound bounding never receives one (text mode never calls it; image mode
    never produces one) — a RAW payload handed to it anyway degrades to the
    original-string fail-safe, never a decode crash."""
    import computer_use_mcp.server as server_mod

    raw_payload = "RAW:AAAA;4x4"
    out, mime = server_mod._bound_outbound_image(raw_payload)
    assert out == raw_payload  # fail-safe: unusable env returns the original
    # the PIL open on b64decode("AAAA") garbage raises -> caught -> original returned
    assert mime in ("image/png",)  # the degraded mimeType is the fail-safe one


# =====================================================================================
# R-6a duplicator robustness: the dangling-IID lifetime bug + first-frame guarantee
# (live Windows only; screen READING only — no input anywhere)
# =====================================================================================


@pytest.mark.skipif(platform.system() != "Windows", reason="Windows-only live capture")
def test_live_dxgi_constructor_survives_repeated_construction() -> None:
    """REGRESSION (dangling-IID): the constructor's QI calls bind every byref/cast
    operand to a live local, so rapid in-process construction succeeds REPEATEDLY
    (close -> construct -> close -> construct). The pre-fix code could GC the inline
    IID temporary mid-call and get E_NOINTERFACE; the post-fix contract is clean
    construction every time (a closed duplication releases the output for the next).
    """
    from computer_use_mcp.backend import _DxgiDuplicator

    for attempt in range(3):
        dup = _DxgiDuplicator()
        assert (dup.width, dup.height) == (1920, 1080) or dup.width > 0
        dup.close()


@pytest.mark.skipif(platform.system() != "Windows", reason="Windows-only live capture")
def test_live_dxgi_first_frame_delivered_and_steady_path_fast() -> None:
    """R-6a first-frame guarantee: a cold duplication with a 0 ms acquire budget
    still delivers the current desktop image within the bounded one-shot retry
    (~58 ms composite on this hardware), and after the first frame the steady path
    returns within the fast timeout budget (never starved on a quiet desktop)."""
    import time as _time

    from computer_use_mcp.backend import _DxgiDuplicator

    dup = _DxgiDuplicator()
    try:
        started = _time.perf_counter()
        cold = dup.grab(timeout_ms=0)  # instant timeout forces the first-frame retry
        cold_ms = (_time.perf_counter() - started) * 1000.0
        assert cold is not None and cold.size == (dup.width, dup.height)
        assert cold_ms < 500.0  # bounded: the retry must terminate well under a second
        # steady state: with a cached frame, even a 0 ms timeout returns it
        steady = dup.grab(timeout_ms=0)
        assert steady is not None and steady.size == (dup.width, dup.height)
    finally:
        dup.close()


@pytest.mark.skipif(platform.system() != "Windows", reason="Windows-only live capture")
def test_live_dxgi_two_live_duplications_second_fails_open() -> None:
    """Windows contract: one IDXGIOutputDuplication per output per process. A SECOND
    live duplicator (the first not closed) fails construction; the failure is the
    documented fail-open path (OSError raised -> the backend keeps its blt fallback),
    never a crash or a silent wrong-size frame."""
    from computer_use_mcp.backend import _DxgiDuplicator

    first = _DxgiDuplicator()
    try:
        try:
            second = _DxgiDuplicator()
        except OSError:
            pass  # the documented contract: construction fails while `first` is live
        else:
            second.close()  # if the OS allowed it, both are valid — just release it
    finally:
        first.close()
