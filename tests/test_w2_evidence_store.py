"""WP5 (ORVEX-2026-108 §2) — the image evidence store and ``computer_fetch_image``.

Ported from the dev copy's ``test_w2_evidence_store.py`` (ORVEX-2026-102 §6 Phase 2)
and ADAPTED to the unified reply contract + the stable retention rule.  The
adaptations, each recorded in ``.orvex/mission/artifacts/gates/wp5-notes.md``:

* **Retention rule (reversed vs dev):** a frame is retained iff THIS reply carries
  it.  Dev kept every capture and only flagged the attachment (SPEC 2.3 state
  "captured+saved+not sent"); the stable contract forbids the K-75 overclaim —
  "no image in this reply" IS "nothing kept".  The dev K-3 anchor tests and the
  ``include_screenshot_after=False``-still-stores tests are therefore REPLACED by
  ``test_a_reply_that_carries_no_image_stores_nothing``.
* **Fetch rejections (§1.1):** ``{ok:false, result:"rejected", reasons:[<token>],``
  ``message}`` — never dev's bare ``{ok:false, error:...}`` (which carries no
  ``result`` token and fails ``validate_reply``).  Every fetch reply in this file
  passes ``validate_reply(reply, "fetch_image")``.
* **Text sessions:** a text-mode reply carries no image, so it stores nothing and
  carries NO evidence keys at all (dev asserted refusal keys on the reply; the
  store-level refusals are still pinned here via the store's own API).
* **Plans:** the plan lease does not exist in the stable tree yet (WP7).  The dev
  lease-refusal test is NOT ported — ``computer_fetch_image`` must stay ALLOWED
  during a lease (S-04); the seam is pinned by
  ``test_the_two_wp7_seam_markers_exist_exactly_once``.
* **``stop_session`` is ASYNC** (WP6 DELTA 1/F4): every direct caller awaits it.

Spec clauses still pinned here: the explicit bounded retention policy (16 frames /
24 MB / 3600 s, FIFO, disposed exactly once at session end); the retrieval tool
addressed by id with the mandatory observe -> id -> fetch scenario; the four
distinguishable outcomes (served / retired-by-a-policy / never-existed ≡ foreign id);
monotonic expiry; session-scoped isolation; the disposal idempotence.
"""

from __future__ import annotations

import asyncio
import base64
import inspect
import io
import json
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from reply_contract import validate_reply
from test_controller_integration import (  # noqa: F401 - pytest fixture import
    FAST_LIMITS,
    FakeComputerBackend,
    ScriptedBackend,
    execute_payload,
    fresh_server,
    make_session,
)

from computer_use_mcp import cli as cli_module
from computer_use_mcp import evidence as evidence_module
from computer_use_mcp import server

SERVER_PATH = Path(server.__file__)

#: The shipped retention bounds, restated here as literals so a silent change to a
#: constant fails a test instead of quietly re-scoping the policy.
EXPECTED_MAX_FRAMES = 16
EXPECTED_MAX_BYTES = 24 * 1024 * 1024
EXPECTED_MAX_AGE_SECONDS = 3600.0


# --- harness ------------------------------------------------------------------------------


class _Clock:
    """Injectable monotonic clock: the 1 h age cap is proven without waiting an hour."""

    def __init__(self, start: float = 10_000.0) -> None:
        self.now = start
        self.calls = 0

    def __call__(self) -> float:
        self.calls += 1
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def observe_metadata(result: Any) -> dict[str, Any]:
    """The JSON payload inside a tool's single TextContent block."""
    assert isinstance(result, list), result
    return json.loads(result[0].text)


def fetch_metadata(result: Any) -> dict[str, Any]:
    return observe_metadata(result)


def image_blocks(result: Any) -> list[Any]:
    return [block for block in result if getattr(block, "type", None) == "image"]


def store_of(bundle: Any) -> Any:
    return bundle.extra[evidence_module.EVIDENCE_STORE_KEY]


def _fresh_store(
    session_id: str,
    *,
    image_delivery: str = "image",
    clock: Any = None,
    max_frames: int = EXPECTED_MAX_FRAMES,
    max_bytes: int = EXPECTED_MAX_BYTES,
    max_age_seconds: float = EXPECTED_MAX_AGE_SECONDS,
) -> Any:
    return evidence_module.EvidenceStore(
        session_id,
        image_delivery=image_delivery,
        clock=clock,
        max_frames=max_frames,
        max_bytes=max_bytes,
        max_age_seconds=max_age_seconds,
    )


def _put(store: Any, marker: str, **kwargs: Any) -> dict[str, Any]:
    """Store one synthetic frame (a pre-ladder PNG, tagged by marker for readability)."""
    return store.store(
        _png_for(marker),
        source=kwargs.pop("source", "computer_observe"),
        attached=kwargs.pop("attached", False),
        width=kwargs.pop("width", 64),
        height=kwargs.pop("height", 48),
        digest=kwargs.pop("digest", "d" * 64),
        observation_id=kwargs.pop("observation_id", "obs_test"),
        **kwargs,
    )


def _retire_by_count_cap(store: Any, image_id: str) -> None:
    """Retire ``image_id`` through the REAL count cap (no clock, no private poking)."""
    original = store.max_frames
    try:
        store.max_frames = 1
        _put(store, "b")
    finally:
        store.max_frames = original
    assert store.lookup(image_id).outcome == "expired"


def _png_for(marker: str) -> str:
    """A tiny real PNG; the marker rides in a text chunk so distinct markers produce
    distinct bytes without inventing an invalid image colour."""
    from PIL import Image, PngImagePlugin

    info = PngImagePlugin.PngInfo()
    info.add_text("marker", marker)
    image = Image.new("RGB", (8, 8), (32, 64, 96))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", pnginfo=info)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def _run(coro: Any) -> Any:
    """Run one coroutine from a sync test (private loop; the async tests use await)."""
    return asyncio.run(coro)


def fetch_result(session_id: str, image_id: str) -> Any:
    """The fetch tool (ASYNC since DELTA 1 / VAL-WP5 F5) awaited from a SYNC test
    body or a worker thread via a private loop."""
    return asyncio.run(server.computer_fetch_image(session_id, image_id))


def _assert_fetch_rejection(reply: Any, token: str) -> dict[str, Any]:
    """A fetch rejection is a §1.1 rejected reply whose ``reasons`` name the token."""
    assert isinstance(reply, dict), reply
    validate_reply(reply, "fetch_image")
    assert reply["ok"] is False
    assert reply["result"] == "rejected"
    assert reply["reasons"] == [token]
    assert "error" not in reply
    assert image_blocks(reply) == []
    return reply


# --- the store's retention policy -----------------------------------------------------------


def test_the_shipped_retention_bounds_are_the_ones_the_mission_names() -> None:
    assert evidence_module.EVIDENCE_MAX_FRAMES == EXPECTED_MAX_FRAMES
    assert evidence_module.EVIDENCE_MAX_BYTES == EXPECTED_MAX_BYTES
    assert evidence_module.EVIDENCE_MAX_AGE_SECONDS == EXPECTED_MAX_AGE_SECONDS


def test_store_keeps_sixteen_frames_and_evicts_the_seventeenth() -> None:
    """The count cap is 16 per session, FIFO by insertion."""
    store = _fresh_store("s-count")
    ids = [_put(store, chr(97 + index))["image_id"] for index in range(EXPECTED_MAX_FRAMES)]
    assert len(ids) == EXPECTED_MAX_FRAMES
    assert store.frame_count == EXPECTED_MAX_FRAMES
    for image_id in ids:
        assert store.lookup(image_id).outcome == "found"

    # The 17th frame evicts the OLDEST, and only the oldest.
    fresh = _put(store, "z")["image_id"]
    assert store.frame_count == EXPECTED_MAX_FRAMES
    assert store.lookup(ids[0]).outcome == "expired"
    assert store.lookup(ids[0]).reason == evidence_module.RETIRE_COUNT_CAP
    assert store.lookup(fresh).outcome == "found"
    assert store.lookup(ids[1]).outcome == "found"


def test_store_evicts_by_bytes_and_refuses_a_frame_bigger_than_the_whole_cap() -> None:
    """The 24 MB cap is a CEILING, not a target (priced off the PRE-ladder size)."""
    store = _fresh_store("s-bytes", max_frames=100, max_bytes=1000)
    first = _put(store, "a")["image_id"]
    # Each synthetic PNG is a few hundred bytes, so 40 of them cannot fit under 1000
    # and the BYTE cap (not the count cap) is what retires the oldest.
    for _ in range(40):
        _put(store, "a")
    assert store.total_bytes <= 1000
    assert store.frame_count < 40, "the byte cap must have retired frames"
    assert store.lookup(first).outcome == "expired"
    assert store.lookup(first).reason == evidence_module.RETIRE_BYTE_CAP

    oversized = _fresh_store("s-huge", max_bytes=10)
    receipt = oversized.store(
        _png_for("q"), source="computer_observe", attached=False, width=1, height=1
    )
    assert receipt["image_stored"] is False
    assert receipt["image_not_stored_reason"] == evidence_module.EVIDENCE_REFUSE_FRAME_TOO_LARGE
    assert "image_id" not in receipt
    assert oversized.frame_count == 0


def test_age_cap_expires_a_frame_using_the_monotonic_clock() -> None:
    """1 h age cap; the sweep runs on WRITE and on ACCESS."""
    clock = _Clock()
    store = _fresh_store("s-age", clock=clock)
    image_id = _put(store, "a")["image_id"]
    clock.advance(EXPECTED_MAX_AGE_SECONDS - 1.0)
    assert store.lookup(image_id).outcome == "found"
    clock.advance(2.0)
    expired = store.lookup(image_id)
    assert expired.outcome == "expired"
    assert expired.reason == evidence_module.RETIRE_AGE_CAP
    assert store.frame_count == 0


def test_age_cap_uses_monotonic_not_wall_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Expiry is a MONOTONIC decision and a wall clock can never drive it.

    Row 1 — the wall clock is pinned to a 1970 stamp and the monotonic clock is
    frozen: the frame is NOT expired, so no wall-clock value can expire anything.
    Row 2 — the wall clock is pinned and the monotonic clock jumps a year: the frame
    IS expired, so the monotonic clock alone decided it."""
    pinned: list[float] = [0.0]
    monkeypatch.setattr(evidence_module, "_wall_clock", lambda: pinned[0])

    frozen_monotonic = _fresh_store("s-clock-frozen", clock=_Clock())
    kept = _put(frozen_monotonic, "a")["image_id"]
    assert frozen_monotonic.lookup(kept).outcome == "found"
    assert frozen_monotonic.lookup(kept).frame is not None
    assert frozen_monotonic.lookup(kept).frame.captured_at.startswith("1970-01-01")

    jumping = _Clock()
    store = _fresh_store("s-clock-jump", clock=jumping)
    expired_id = _put(store, "a")["image_id"]
    jumping.advance(365 * 24 * 3600.0)
    assert store.lookup(expired_id).outcome == "expired"


def test_retired_ids_report_expired_never_not_found() -> None:
    """"No longer available" must be DISTINGUISHABLE from "never existed"."""
    store = _fresh_store("s-tomb", max_frames=1)
    first = _put(store, "a")["image_id"]
    _put(store, "b")
    assert store.lookup(first).outcome == "expired"
    assert store.lookup("img_never_existed_at_all").outcome == "not_found"


# --- the RAW: refusal (the D1 image-block killer) ---------------------------------------------


def test_a_raw_key_payload_is_refused_even_on_an_image_session() -> None:
    """A ``RAW:`` payload is refused wherever it arrives, and issues NO id."""
    store = _fresh_store("s-raw", image_delivery="image")
    receipt = store.store(
        "RAW:" + base64.b64encode(b"\x02" * 32).decode("ascii") + ";8x8",
        source="computer_observe",
        attached=False,
        width=8,
        height=8,
    )
    assert receipt["image_stored"] is False
    assert receipt["image_not_stored_reason"] == evidence_module.EVIDENCE_REFUSE_TEXT_SESSION
    assert "image_id" not in receipt
    assert store.frame_count == 0


def test_text_session_with_real_pngs_is_still_refused() -> None:
    """``start_session`` swallows a backend that rejects raw-payload-key arming.  A
    text session that then produces PNGs must STILL store nothing — serving one as
    an image block is the D1 killer regardless of what the payload happens to be."""
    store = _fresh_store("s-text-png", image_delivery="text")
    receipt = store.store(_png_for("p"), source="computer_observe", attached=False, width=8, height=8)
    assert receipt["image_stored"] is False
    assert receipt["image_not_stored_reason"] == evidence_module.EVIDENCE_REFUSE_TEXT_SESSION
    assert store.frame_count == 0


def test_a_text_session_reply_stores_nothing_and_carries_no_evidence_keys(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A text-mode reply carries NO image, so NOTHING is stored and the reply carries
    no evidence keys at all — a capture that was never shown is never claimed."""
    session_id, bundle, backend, _ = make_session(
        monkeypatch,
        dry_run=False,
        require_approval=False,
        limits=FAST_LIMITS,
        image_delivery="text",
    )
    store = store_of(bundle)
    assert store.image_delivery == "text"

    metadata = observe_metadata(server.computer_observe(session_id))
    assert metadata["image_format"] == "none"
    for key in (
        "image_stored",
        "image_id",
        "image_not_stored_reason",
        "evidence_fetch",
        "evidence_retention",
    ):
        assert key not in metadata, key
    assert store.frame_count == 0
    assert list(store.image_ids()) == []
    assert backend is not None  # the text-mode capture happened; it was just not kept

    # Nothing was ever issued, so the fetch tool cannot be handed one either.
    fetched = fetch_result(session_id, "img_0000000000000000")
    _assert_fetch_rejection(fetched, "image_not_found")


# --- store-key lifetime -------------------------------------------------------------------


def test_evidence_store_key_is_written_at_bundle_construction(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The key exists BEFORE any capture, never lazily on first use — a lazily-created
    key could be re-created by a tool call resuming after a teardown popped the
    bundle from ``_bundles``."""
    session_id, bundle, _backend, _ = make_session(
        monkeypatch, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    store = store_of(bundle)
    assert isinstance(store, evidence_module.EvidenceStore)
    assert store.session_id == session_id
    assert store.disposed is False
    assert store.frame_count == 0
    assert evidence_module.EVIDENCE_STORE_KEY in server._bundles[session_id].extra


def test_stop_session_disposes_the_store_and_empties_it(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Disposal is a REAL teardown, not just a pop: the store object is emptied and
    marked disposed, a later write on it is refused, and a later fetch is refused."""
    session_id, bundle, _backend, _ = make_session(
        monkeypatch, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    store = store_of(bundle)
    server.computer_observe(session_id)
    assert store.frame_count == 1

    _run(server.stop_session(session_id))
    assert "evidence_store" not in bundle.extra
    assert store.disposed is True
    assert store.frame_count == 0
    assert store.total_bytes == 0
    assert list(store.image_ids()) == []
    receipt = store.store(
        _png_for("x"), source="computer_observe", attached=False, width=1, height=1
    )
    assert receipt["image_stored"] is False
    assert receipt["image_not_stored_reason"] == evidence_module.EVIDENCE_REFUSE_DISPOSED


def test_stop_during_computer_execute_does_not_resurrect_the_store(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The resurrection race, reproduced deterministically.

    The teardown lands from INSIDE the backend's execute (the same ordering as a
    ``stop_session`` that completes while ``computer_execute`` — the async tool — is
    suspended at its await; ``stop_session`` itself is async now, so the test drives
    the shared kill path ``_close_stopped_bundle`` directly).  The response builder
    reaches the evidence hook AFTER the bundle was popped: the hook must add nothing
    and must NOT re-create the key on a bundle ``_bundles`` no longer holds."""

    session_id, bundle, _backend, _ = make_session(
        monkeypatch, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    store = store_of(bundle)

    class _StopsItselfBackend(ScriptedBackend):
        def execute(self, action: Any, stop: Any = None) -> str:
            message = super().execute(action, stop)
            if not self.stopped_once:
                self.stopped_once = True
                server._close_stopped_bundle(target, server._bundles[target], source="stop_session")
            return message

        stopped_once = False

    target = session_id
    backend = _StopsItselfBackend()
    monkeypatch.setattr(server, "_backend_factory", lambda: backend)
    bundle.backend = backend
    bundle.agent.backend = backend  # the agent holds the same backend reference

    result = _run(server.computer_execute(session_id, "click", x=10, y=10, approved=True))
    payload = execute_payload(result)
    assert backend.stopped_once is True, "the teardown must have landed mid-execute"
    assert "image_id" not in payload
    assert "image_stored" not in payload
    assert "evidence_store" not in bundle.extra
    assert store.disposed is True
    assert store.frame_count == 0
    with pytest.raises(server._StoppedSession):
        server._get_live_bundle(session_id)


def test_the_hook_never_creates_a_store_that_is_not_there(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The resurrection guard itself: a hook on a bundle whose key is gone writes
    nothing and creates nothing."""
    _session_id, bundle, _backend, _ = make_session(
        monkeypatch, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    bundle.extra.pop(evidence_module.EVIDENCE_STORE_KEY, None)
    assert evidence_module.register_frame(
        bundle.extra,
        payload=_png_for("k"),
        source="computer_execute",
        attached=False,
        width=8,
        height=8,
    ) == {}
    assert evidence_module.EVIDENCE_STORE_KEY not in bundle.extra


# --- the mandatory scenario -------------------------------------------------------------------


async def test_the_mandatory_scenario_observe_to_id_to_fetch(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Observe -> an id -> you decide you must look again -> fetch."""
    session_id, _bundle, _backend, _ = make_session(
        monkeypatch, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    first = observe_metadata(server.computer_observe(session_id))
    image_id = first["image_id"]
    assert image_id.startswith("img_")

    fetched = await server.computer_fetch_image(session_id, image_id)
    validate_reply(fetch_metadata(fetched), "fetch_image")
    blocks = image_blocks(fetched)
    assert len(blocks) == 1
    assert blocks[0].mimeType in ("image/png", "image/jpeg")
    meta = fetch_metadata(fetched)
    assert meta["ok"] is True and meta["result"] == "verified"
    assert meta["image_id"] == image_id
    assert meta["captured_at"] == first["image_captured_at"]
    assert meta["width"] == first["image_width"]
    assert meta["height"] == first["image_height"]
    assert meta["observation_id"] == first["observation"]["observation_id"]
    assert meta["digest"] == first["digest"]
    assert meta["age_seconds"] >= 0.0
    assert meta["image_outbound_scale"] is None or 0.0 < meta["image_outbound_scale"] < 1.0
    assert "not necessarily current" in meta["freshness_note"].lower()


async def test_fetch_twice_returns_byte_identical_bytes(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Determinism: the same stored frame served twice is the same image."""
    session_id, _bundle, _backend, _ = make_session(
        monkeypatch, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    image_id = observe_metadata(server.computer_observe(session_id))["image_id"]
    one = await server.computer_fetch_image(session_id, image_id)
    two = await server.computer_fetch_image(session_id, image_id)
    assert image_blocks(one)[0].data == image_blocks(two)[0].data
    assert image_blocks(one)[0].mimeType == image_blocks(two)[0].mimeType
    assert fetch_metadata(one)["image_id"] == fetch_metadata(two)["image_id"]


async def test_a_png_under_budget_is_stored_and_served_untouched(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The PRE-ladder original is what is stored: a frame under the outbound budget is
    returned byte-for-byte, never re-encoded, and the fetch reports NO applied
    downscale (None = pass-through — the §1.1 MINOR-b convention)."""
    session_id, bundle, backend, _ = make_session(
        monkeypatch, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    server.computer_observe(session_id)
    store = store_of(bundle)
    stored = store.frames()[0]
    assert stored.image_base64 == backend.observed[-1].image_base64
    image_id = stored.image_id
    served = image_blocks(await server.computer_fetch_image(session_id, image_id))[0]
    assert served.mimeType == "image/png"
    assert served.data == stored.image_base64
    assert (
        fetch_metadata(await server.computer_fetch_image(session_id, image_id))["image_outbound_scale"]
        is None
    )


# --- only reply-bearing captures are kept ---------------------------------------------------


async def test_an_executed_reply_that_carries_the_image_stores_and_names_it(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The execute hook: the reply carries the post-action image, so the frame is
    retained and the reply carries the additive keys (and nothing else changed)."""
    session_id, bundle, _backend, _ = make_session(
        monkeypatch, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    result = await server.computer_execute(session_id, "click", x=10, y=10, approved=True)
    assert len(image_blocks(result)) == 1
    payload = execute_payload(result)
    assert payload["image_stored"] is True
    assert payload["image_attached"] is True
    assert payload["image_id"].startswith("img_")
    assert payload["evidence_retention"]["max_frames"] == EXPECTED_MAX_FRAMES
    assert "computer_fetch_image" in payload["evidence_fetch"]
    assert store_of(bundle).lookup(payload["image_id"]).outcome == "found"
    fetched = await server.computer_fetch_image(session_id, payload["image_id"])
    assert len(image_blocks(fetched)) == 1


async def test_a_reply_that_carries_no_image_stores_nothing(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stable retention rule: ONLY captures a reply actually carries are kept.

    Three image-less replies, three kinds of "no image": the host opt-out
    (``include_screenshot_after=False``), a rejection (no post-action capture at
    all), and text mode.  None stores anything, none carries evidence keys."""
    session_id, bundle, _backend, _ = make_session(
        monkeypatch, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    store = store_of(bundle)

    opted_out = execute_payload(
        await server.computer_execute(
            session_id, "click", x=10, y=10, approved=True, include_screenshot_after=False
        )
    )
    assert "screenshot_after_base64" not in opted_out
    for key in ("image_stored", "image_id", "image_attached", "evidence_fetch"):
        assert key not in opted_out, key

    rejected = execute_payload(
        await server.computer_execute(session_id, "drag", x=10, y=10, approved=True)
    )
    assert rejected["ok"] is False
    for key in ("image_stored", "image_id", "evidence_fetch"):
        assert key not in rejected, key

    text_metadata = observe_metadata(
        # a text-mode session of ANOTHER id would be a different store; use the same
        # session's rejection paths and then a fresh text session for the mode check.
        _observe_text_session(monkeypatch)
    )
    assert "image_stored" not in text_metadata

    assert store.frame_count == 0
    assert store.total_bytes == 0
    assert list(store.image_ids()) == []


def _observe_text_session(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """One text-mode session's observe reply (blocks), for the no-image no-store pin."""
    session_id, _bundle, _backend, _ = make_session(
        monkeypatch,
        dry_run=False,
        require_approval=False,
        limits=FAST_LIMITS,
        image_delivery="text",
    )
    return server.computer_observe(session_id)


# --- the four distinguishable fetch outcomes ------------------------------------------------


async def test_unknown_image_id_reports_image_not_found(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id, _bundle, _backend, _ = make_session(
        monkeypatch, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    reply = await server.computer_fetch_image(session_id, "img_0000000000000000")
    _assert_fetch_rejection(reply, "image_not_found")
    assert "img_0000000000000000" not in json.dumps(reply)


def test_foreign_session_id_is_byte_identical_to_unknown(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A valid id from ANOTHER session is indistinguishable from one that never
    existed, byte for byte — a wrong id can never probe another session's images."""
    first, _b1, _be1, _ = make_session(
        monkeypatch, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    second, _b2, _be2, _ = make_session(
        monkeypatch, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    foreign_id = observe_metadata(server.computer_observe(first))["image_id"]

    unknown = fetch_result(second, "img_deadbeefdeadbeef")
    foreign = fetch_result(second, foreign_id)
    assert json.dumps(unknown, sort_keys=True) == json.dumps(foreign, sort_keys=True)
    _assert_fetch_rejection(foreign, "image_not_found")


def test_expired_image_id_reports_image_expired_naming_the_policy(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id, bundle, _backend, _ = make_session(
        monkeypatch, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    image_id = observe_metadata(server.computer_observe(session_id))["image_id"]
    store = store_of(bundle)
    # Retire it through the REAL count cap: deterministic, no clock needed.
    _retire_by_count_cap(store, image_id)
    reply = fetch_result(session_id, image_id)
    _assert_fetch_rejection(reply, "image_expired")
    assert reply["retired_by"] == "count_cap"
    assert str(EXPECTED_MAX_FRAMES) in reply["message"]
    assert str(EXPECTED_MAX_BYTES) in reply["message"]
    assert str(EXPECTED_MAX_AGE_SECONDS) in reply["message"]


async def test_expired_by_the_age_cap_reports_the_age_policy(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The age cap fires on ACCESS: a patched monotonic clock past 3600 s turns the
    next fetch into ``image_expired`` retired by the age cap."""
    # The clock is injected BEFORE the session exists, so the store built at bundle
    # construction captures its monotonic start from the fake too.
    clock = _Clock(start=time.monotonic())
    monkeypatch.setattr(evidence_module, "_monotonic", clock)
    session_id, _bundle, _backend, _ = make_session(
        monkeypatch, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    image_id = observe_metadata(server.computer_observe(session_id))["image_id"]
    clock.advance(EXPECTED_MAX_AGE_SECONDS + 1.0)
    reply = await server.computer_fetch_image(session_id, image_id)
    _assert_fetch_rejection(reply, "image_expired")
    assert reply["retired_by"] == "age_cap"
    assert "3600" in reply["message"]


def test_fetch_on_a_stopped_session_reports_session_stopped(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id, _bundle, _backend, _ = make_session(
        monkeypatch, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    image_id = observe_metadata(server.computer_observe(session_id))["image_id"]
    _run(server.stop_session(session_id))
    reply = fetch_result(session_id, image_id)
    assert isinstance(reply, dict)
    assert reply["ok"] is False
    assert reply["error"] == "session_stopped"
    assert image_blocks(reply) == []


def test_fetch_on_an_unknown_session_reports_unknown_session(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    reply = fetch_result("no-such-session", "img_0000000000000000")
    assert isinstance(reply, dict)
    assert reply["ok"] is False
    assert reply["error"] == "unknown_session"


def test_a_disposed_store_on_a_live_bundle_reports_store_disposed(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mid-teardown honesty: a live bundle whose store key is already gone (the
    exactly-once disposal popped it) answers ``store_disposed`` — never a partial
    image, never a resurrection."""
    session_id, bundle, _backend, _ = make_session(
        monkeypatch, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    image_id = observe_metadata(server.computer_observe(session_id))["image_id"]
    evidence_module.dispose_store(bundle.extra)  # simulate the mid-teardown state
    reply = fetch_result(session_id, image_id)
    _assert_fetch_rejection(reply, "store_disposed")


def test_frame_too_large_is_a_typed_fail_closed_fetch_refusal(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A frame larger than the session's byte ceiling can never be served: the typed
    ``frame_too_large`` refusal, fail-closed (unreachable through ``store()``, which
    refuses oversize payloads before an id ever exists)."""
    session_id, bundle, _backend, _ = make_session(
        monkeypatch, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    image_id = observe_metadata(server.computer_observe(session_id))["image_id"]
    store = store_of(bundle)
    store.max_bytes = 10  # shrink the ceiling AFTER the frame was legitimately stored
    reply = fetch_result(session_id, image_id)
    _assert_fetch_rejection(reply, "frame_too_large")
    assert reply["evidence_retention"]["max_bytes"] == 10


async def test_a_malformed_image_id_is_byte_identical_to_an_unknown_one(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A garbage id must not become a side channel: empty, non-string, and
    never-existed all answer the same way."""
    session_id, _bundle, _backend, _ = make_session(
        monkeypatch, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    unknown = await server.computer_fetch_image(session_id, "img_0000000000000000")
    for value in ("", "   ", "not-an-id", 12345, None, ["img_x"]):
        other = await server.computer_fetch_image(session_id, value)  # type: ignore[arg-type]
        assert json.dumps(other, sort_keys=True) == json.dumps(unknown, sort_keys=True), value
    _assert_fetch_rejection(unknown, "image_not_found")


def test_lookup_observes_disposal_atomically() -> None:
    """VAL-WP5 F3: the ``_disposed`` check shares the store lock with the frame read,
    so a lookup racing the teardown answers ``disposed`` — never ``not_found`` for a
    frame that existed a moment ago."""
    store = _fresh_store("s-atomic")
    image_id = _put(store, "a")["image_id"]
    store.dispose()
    lookup = store.lookup(image_id)
    assert lookup.outcome == "disposed"
    assert lookup.reason == evidence_module.EVIDENCE_REFUSE_DISPOSED


# --- VAL-WP5 F1: the stored bytes are EXACTLY the bytes the reply carried ------


async def test_an_observe_grid_reply_stores_and_fetches_the_carried_grid_bytes(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A derived visual view (grid) changes the pixels the reply carries, so the
    store keeps the DERIVED image, not the raw capture.  Compared precisely: both
    the reply's image block and the fetched image block are
    ``_bound_outbound_image(encode_png_b64(grid_render))`` run on the SAME pre-ladder
    bytes — so the two base64 payloads must be byte-identical — and the fetch reply
    names the view."""
    session_id, bundle, backend, _ = make_session(
        monkeypatch, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    blocks = server.computer_observe(session_id, visual_view="grid")
    reply_meta = observe_metadata(blocks)
    assert reply_meta["visual_view"] == "grid"
    assert len(image_blocks(blocks)) == 1

    # The STORED bytes are the carried derived image, not the raw capture.
    store = store_of(bundle)
    stored = store.frames()[0]
    assert stored.visual_view == "grid"
    view_provider = server.resolve_view("grid")
    derived = view_provider.derive(backend.observed[-1], density=server.resolve_density(None))
    assert stored.image_base64 == server.encode_png_b64(derived.image)
    assert stored.image_base64 != backend.observed[-1].image_base64, (
        "the derived grid bytes must differ from the raw capture"
    )

    fetched = await server.computer_fetch_image(session_id, reply_meta["image_id"])
    fetched_meta = fetch_metadata(fetched)
    validate_reply(fetched_meta, "fetch_image")
    # Byte-identity: same pre-ladder input, same outbound ladder, same bytes.
    assert image_blocks(fetched)[0].data == image_blocks(blocks)[0].data
    assert fetched_meta["visual_view"] == "grid"


async def test_an_execute_grid_reply_stores_and_fetches_the_carried_view_bytes(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The execute hook stores the carried DERIVED view payload (not the raw
    post-action capture), and the fetch reply names the view.  Byte-identity is
    precise: the reply image block and the fetched image block are both
    ``_bound_outbound_image(view_payload[0])`` — the identical base64.  The stored
    bytes also differ from EVERY raw capture the fake produced this session."""
    session_id, bundle, backend, _ = make_session(
        monkeypatch, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    blocks = await server.computer_execute(
        session_id,
        "click",
        x=10,
        y=10,
        approved=True,
        visual_view="grid",
        # The full-image opt-in: the reply's image block is then EXACTLY
        # ``_bound_outbound_image(view_payload[0])`` — the SAME ladder the fetch
        # re-runs on the stored bytes.  (The DEFAULT execute reply ships the
        # half-res JPEG encode instead, by design; what is compared here is stated:
        # reply image bytes == fetched image bytes == the same-ladder output.)
        include_screenshot_after=True,
    )
    payload = execute_payload(blocks)
    assert payload["visual_view"] == "grid"
    assert len(image_blocks(blocks)) == 1

    store = store_of(bundle)
    stored = store.frames()[0]
    assert stored.visual_view == "grid"
    assert all(
        stored.image_base64 != observation.image_base64 for observation in backend.observed
    ), "the stored bytes must be the derived view, never a raw capture"

    fetched = await server.computer_fetch_image(session_id, payload["image_id"])
    fetched_meta = fetch_metadata(fetched)
    validate_reply(fetched_meta, "fetch_image")
    assert image_blocks(fetched)[0].data == image_blocks(blocks)[0].data
    assert fetched_meta["visual_view"] == "grid"


# --- VAL-WP5 F2: a late view failure stores nothing ----------------------------


def test_a_late_spatial_text_failure_stores_nothing(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The observe hook runs at the FINAL return, after the spatial-text block: a
    ``_view_derive_failed`` raised AFTER the capture leaves no orphan frame and no
    undisclosed id — an image-less reply never stores anything."""
    session_id, bundle, _backend, _ = make_session(
        monkeypatch, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    store = store_of(bundle)

    from computer_use_mcp.visual_views import VisualViewError

    def _explode(*args: Any, **kwargs: Any) -> None:
        raise VisualViewError("forced after-capture failure")

    monkeypatch.setattr(server, "_spatial_text_for", _explode)
    reply = server.computer_observe(session_id, visual_view="grid")
    assert isinstance(reply, dict)
    assert reply["error"] == "view_derive_failed"
    assert "image_id" not in reply
    assert store.frame_count == 0
    assert store.total_bytes == 0


# --- the WP7 seams --------------------------------------------------------------------------


def test_the_two_wp7_seam_markers_exist_exactly_once() -> None:
    """Exactly one ``WP7-SEAM(fetch-lease)`` (where WP7 keeps the fetch ALLOWED
    during a plan lease, S-04) and one ``WP7-SEAM(evidence-by-backend)`` (where WP7
    builds the plan evidence block from the store + lease, S-03).  The plans
    themselves do not exist yet — WP7 owns them; this pins the seams, not the plans."""
    source = SERVER_PATH.read_text(encoding="utf-8")
    assert source.count("WP7-SEAM(fetch-lease)") == 1
    assert source.count("WP7-SEAM(evidence-by-backend)") == 1


def test_the_store_read_api_for_evidence_by_backend_exists_and_is_backend_agnostic() -> None:
    """S-03 seam: ``EvidenceStore.captures()`` is the read API WP7's
    ``evidence_by_backend`` consumes — bounded metadata, never pixels, and it reads
    the REPLY-FILLED store, so it works identically on every backend (no
    ``backend.executed``/``backend.observed`` fake-only reads anywhere in server.py)."""
    store = _fresh_store("s-captures")
    _put(store, "a", source="computer_zoom", observation_id="obs_1")
    captures = store.captures()
    assert len(captures) == 1
    entry = captures[0]
    assert entry["image_id"].startswith("img_")
    assert entry["source"] == "computer_zoom"
    assert entry["observation_id"] == "obs_1"
    assert set(entry) <= {
        "image_id",
        "source",
        "observation_id",
        "captured_at",
        "width",
        "height",
        "byte_size",
        "crop_region",
        "visual_view",
    }
    assert "image_base64" not in json.dumps(captures)
    # No fake-only backend reads in the evidence path (S-03): the hooks read the store.
    source = SERVER_PATH.read_text(encoding="utf-8")
    assert "backend.executed" not in source
    assert "backend.observed" not in source


def test_captures_are_filled_on_any_backend(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The store is filled by the reply hooks, so BOTH a scripted backend and the
    plain fake backend produce fetchable, listable captures — the S-03 read works on
    both, never through backend-specific attributes."""
    for backend_cls in (ScriptedBackend, FakeComputerBackend):
        session_id, bundle, _backend, _ = make_session(
            monkeypatch,
            backend=backend_cls(),
            dry_run=False,
            require_approval=False,
            limits=FAST_LIMITS,
        )
        metadata = observe_metadata(server.computer_observe(session_id))
        store = store_of(bundle)
        assert store.frame_count == 1
        assert metadata["image_id"] in [entry["image_id"] for entry in store.captures()]
        assert len(image_blocks(fetch_result(session_id, metadata["image_id"]))) == 1
        _run(server.stop_session(session_id))


# --- the wire contract ----------------------------------------------------------------------


def test_fetch_image_publishes_no_output_schema(fresh_server: Any) -> None:
    """The reply is a content-block list, so the annotation MUST be ``Any``.
    ``-> dict[str, object]`` on such a tool crashes the SDK in ``convert_result``."""
    meta = server.mcp._tool_manager.get_tool("computer_fetch_image")
    assert meta is not None
    assert meta.fn_metadata.output_model is None
    assert meta.fn_metadata.output_schema is None
    assert "outputSchema" not in json.dumps(meta.parameters)
    annotation = server.computer_fetch_image.__annotations__["return"]
    assert annotation in (Any, "Any", "typing.Any"), annotation
    assert "dict" not in str(annotation), (
        "a dict annotation on a content-block-returning tool is the D3 crash shape"
    )


def test_fetch_image_is_an_async_def_with_plain_parameters(fresh_server: Any) -> None:
    """Two required non-nullable strings keep the schema free of anyOf/$ref.
    (WP5 DELTA 1 / VAL-WP5 F5: the tool is now ASYNC — the PIL re-encode ladder runs
    off the loop via asyncio.to_thread; the advertised schema is unchanged.)"""
    assert inspect.iscoroutinefunction(server.computer_fetch_image)
    meta = server.mcp._tool_manager.get_tool("computer_fetch_image")
    schema_text = json.dumps(meta.fn_metadata.arg_model.model_json_schema())
    assert "anyOf" not in schema_text
    assert "$ref" not in schema_text and "$defs" not in schema_text
    assert set(meta.fn_metadata.arg_model.model_fields) == {"session_id", "image_id"}


async def test_the_served_surface_is_ten_tools() -> None:
    """WP5 restates the two tool-count pins to 10, never deletes them."""
    names = {tool.name for tool in await server.mcp.list_tools()}
    assert len(names) == 10, sorted(names)
    assert "computer_fetch_image" in names
    assert cli_module.EXPECTED_TOOLS == names
    assert len(cli_module.EXPECTED_TOOLS) == 10


# --- concurrency and leak -------------------------------------------------------------------


def test_two_sessions_store_and_fetch_independently(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Session-scoped isolation under interleaved use."""
    first, bundle_a, _be_a, _ = make_session(
        monkeypatch, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    second, bundle_b, _be_b, _ = make_session(
        monkeypatch, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    ids_a = [observe_metadata(server.computer_observe(first))["image_id"] for _ in range(3)]
    ids_b = [observe_metadata(server.computer_observe(second))["image_id"] for _ in range(3)]
    assert not set(ids_a) & set(ids_b)
    assert store_of(bundle_a) is not store_of(bundle_b)
    for image_id in ids_a:
        assert image_blocks(fetch_result(first, image_id))
        denied = fetch_result(second, image_id)
        _assert_fetch_rejection(denied, "image_not_found")
    for image_id in ids_b:
        assert image_blocks(fetch_result(second, image_id))
    # Stopping one session must not touch the other.
    _run(server.stop_session(first))
    assert store_of(bundle_b).disposed is False
    assert image_blocks(fetch_result(second, ids_b[0]))


def test_concurrent_stores_and_fetches_stay_bounded_and_consistent(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Concurrency soak: 4 sessions x N frames in threads, each frame fetchable."""
    sessions = [
        make_session(monkeypatch, dry_run=False, require_approval=False, limits=FAST_LIMITS)[0]
        for _ in range(4)
    ]
    per_session = 6
    errors: list[str] = []

    def worker(session_id: str) -> None:
        try:
            for _ in range(per_session):
                image_id = observe_metadata(server.computer_observe(session_id))["image_id"]
                if not image_blocks(fetch_result(session_id, image_id)):
                    errors.append(f"{session_id}: fetch returned no image for {image_id}")
        except Exception as exc:  # noqa: BLE001 - the point of the soak
            errors.append(f"{session_id}: {type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=worker, args=(sid,)) for sid in sessions]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30.0)
    assert all(not thread.is_alive() for thread in threads)
    assert errors == []
    for session_id in sessions:
        store = store_of(server._get_bundle(session_id))
        assert store.frame_count == per_session
        assert store.frame_count <= EXPECTED_MAX_FRAMES
        assert store.total_bytes <= EXPECTED_MAX_BYTES


def test_a_fetch_racing_stop_session_never_serves_a_disposed_frame(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The store's OWN lock: a fetch interleaved with the stop is serialized against
    the disposal, so it answers honestly and never serves a frame out of a disposed
    store — and never raises."""
    session_id, bundle, _backend, _ = make_session(
        monkeypatch, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    store = store_of(bundle)
    ids = [observe_metadata(server.computer_observe(session_id))["image_id"] for _ in range(8)]
    outcomes: list[Any] = []
    failures: list[str] = []
    barrier = threading.Barrier(2)

    def fetcher() -> None:
        try:
            barrier.wait(timeout=10)
            for image_id in ids:
                outcomes.append(fetch_result(session_id, image_id))
        except Exception as exc:  # noqa: BLE001 - the hazard under test
            failures.append(f"{type(exc).__name__}: {exc}")

    def stopper() -> None:
        try:
            barrier.wait(timeout=10)
            _run(server.stop_session(session_id))
        except Exception as exc:  # noqa: BLE001
            failures.append(f"stop: {type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=fetcher), threading.Thread(target=stopper)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30.0)
    assert all(not thread.is_alive() for thread in threads)
    assert failures == []
    for outcome in outcomes:
        if isinstance(outcome, dict):
            # A racing fetch was refused; VAL-WP5 F3/F7: the ONLY honest refusals
            # here are the lifecycle ones — ``session_stopped`` (bundle gone; the
            # lifecycle error shape) or ``store_disposed`` (store disposed under the
            # lookup, atomically — the rejected-reply shape).  ``image_not_found``
            # would mean the disposal was observed too late.
            assert outcome["ok"] is False
            token = (
                outcome["reasons"][0] if "reasons" in outcome else outcome.get("error")
            )
            assert token in {"session_stopped", "store_disposed"}, outcome
        else:
            assert image_blocks(outcome)
    assert bundle.extra.get(evidence_module.EVIDENCE_STORE_KEY) is None
    assert store.disposed is True
    assert store.frame_count == 0
    assert list(store.image_ids()) == []


def test_a_store_reference_held_across_a_stop_is_empty_and_refuses(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A caller that kept a reference to the store object sees it emptied and
    refusing, not merely unlinked — and the key is never re-created."""
    session_id, bundle, _backend, _ = make_session(
        monkeypatch, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    held = store_of(bundle)
    image_id = observe_metadata(server.computer_observe(session_id))["image_id"]
    assert image_blocks(fetch_result(session_id, image_id))
    assert held.frame_count == 1

    _run(server.stop_session(session_id))
    assert held.disposed is True
    assert held.frame_count == 0
    assert held.total_bytes == 0
    # VAL-WP5 F3: a disposed store answers ``disposed`` — distinguishable from
    # "never existed" and honest about WHY the frame is gone.
    assert held.lookup(image_id).outcome == "disposed"
    assert evidence_module.EVIDENCE_STORE_KEY not in bundle.extra
    assert evidence_module.evidence_store(bundle.extra) is None


# --- the store's own invariants -------------------------------------------------------------


def test_the_store_carries_no_cross_session_state() -> None:
    store = _fresh_store("s-only")
    assert store.session_id == "s-only"
    assert store.image_delivery == "image"
    assert store.retention() == {
        "max_frames": EXPECTED_MAX_FRAMES,
        "max_bytes": EXPECTED_MAX_BYTES,
        "max_age_seconds": EXPECTED_MAX_AGE_SECONDS,
    }


def test_ids_are_unique_unguessable_and_prefixed() -> None:
    store = _fresh_store("s-ids")
    ids = [_put(store, chr(97 + index % 26))["image_id"] for index in range(8)]
    assert len(set(ids)) == len(ids)
    for image_id in ids:
        assert image_id.startswith("img_")
        assert len(image_id) == len("img_") + 16
        int(image_id[4:], 16)  # hex


def test_a_stored_frame_records_both_clocks_and_its_provenance() -> None:
    """A monotonic start (the expiry clock) AND a wall-clock start (the
    ``captured_at`` the model is shown) plus the provenance it needs to judge age."""
    clock = _Clock()
    store = _fresh_store("s-prov", clock=clock)
    _put(store, "a", source="computer_zoom", observation_id="obs_1", digest="d" * 64)
    frame = store.frames()[0]
    assert frame.source == "computer_zoom"
    assert frame.observation_id == "obs_1"
    assert frame.digest == "d" * 64
    assert frame.captured_monotonic == clock.now
    assert frame.captured_at.endswith("Z")
    # VAL-WP5 F4: the byte cap prices the HELD base64 string (resident memory).
    assert frame.byte_size == len(frame.image_base64)
    assert frame.width == 64 and frame.height == 48
    assert store.age_seconds(frame.image_id) == 0.0
    clock.advance(12.5)
    assert store.age_seconds(frame.image_id) == 12.5


def test_disposing_twice_is_idempotent() -> None:
    store = _fresh_store("s-dispose")
    _put(store, "a")
    assert store.dispose() == 1
    assert store.dispose() == 0
    assert store.disposed is True
    # VAL-WP5 F3: after disposal the lookup answers ``disposed`` — the id's fate is
    # named, never conflated with "never existed".
    assert store.lookup("img_whatever").outcome == "disposed"
