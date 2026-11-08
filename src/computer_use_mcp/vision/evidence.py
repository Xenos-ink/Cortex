"""The per-session image evidence store and ``computer_fetch_image``'s backend.

One sentence first: *an image a reply carries is retrievable by id for the rest of
the session, within explicit bounds* — the reply that carries a capture also carries
its ``image_id``, and ``computer_fetch_image`` re-serves that exact frame later.

The retention policy is explicit and bounded (never grown, never outliving its
session): **16 frames / 24 MB / 3600 s per session**, FIFO by insertion, swept on
write and on access, disposed when the session ends.  The byte ceiling is priced on
what is HELD: the ``len`` of the stored base64 string — the resident cost (VAL-WP5
F4; a full-screen frame measures ~325 KB decoded, ~435 KB as the held base64 str, so
16 of them are ~7 MB resident typical) — 24 MB is a ceiling, not a target.  There
are NO environment knobs: the
bounds are fixed policy values, and the store takes them as constructor arguments so
a test can shrink them instead of reaching into globals.

Load-bearing design decisions:

**Memory only, on purpose.**  A disk store would add a filesystem write surface,
path handling, cleanup-on-crash and a cross-session leak vector to a product that is
otherwise entirely in-process and fail-closed.  The consequence is stated honestly:
the evidence lives exactly as long as the session.  A ``stop_session``, an idle-reap,
a start failure or a restart loses every retained frame, and a retired id answers
with a clear ``image_expired`` rather than silently empty bytes.

**A text-delivery session has no pixels.**  ``start_session(image_delivery="text")``
arms the backend's raw-pixel-key mode, so a capture payload can be the placeholder
string ``RAW:<sha>;WxH`` instead of a PNG.  Serving that string as ``image/png`` puts
a malformed image block into a model history and gets the whole provider request
rejected with a 400 — permanently killing the session.  The store therefore refuses
to store anything for a text session (and refuses any ``RAW:`` payload anywhere, via
the existing ``backend._payload_is_raw_key`` detector) and issues **no id at all**, so
a fetch can never be handed one.  (The server's hooks additionally store nothing on
any reply that carries no image, so a text session never even reaches the store.)

**The store key is written once, at bundle construction, and this module owns its own
lock.**  ``bundle.extra`` is an unsynchronised dict that is never copied; a teardown
can run while a suspended tool call holds the bundle.  Every function here that
touches ``extra`` only READS the key, and :func:`dispose_store` is the single place
that removes it — so a hook that runs after the teardown silently writes nothing
instead of resurrecting a store on a dead bundle.  The store's own ``threading.Lock``
guards the frame table, the byte counter and the age sweep; a lock on ``extra`` could
not protect any of them.

**Expiry is a monotonic decision.**  The wall clock is only ever read to stamp the
human-readable ``captured_at``; a clock adjustment can misreport an age but can never
retire a frame early or keep one alive.
"""

from __future__ import annotations

import base64
import threading
import time
import uuid
import weakref
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Literal

from ..desktop.capture import _payload_is_raw_key  # noqa: SLF001 - the RAW-payload detector

#: The ``bundle.extra`` key the store lives under.  Written ONCE at bundle
#: construction (``start_session``) and removed only by :func:`dispose_store`.
EVIDENCE_STORE_KEY = "evidence_store"

#: Frames retained per session.  16 covers a 10-point plan plus retries without
#: unbounded growth.
EVIDENCE_MAX_FRAMES = 16

#: Bytes retained per session, priced on the HELD base64 length (see module docstring).
EVIDENCE_MAX_BYTES = 24 * 1024 * 1024

#: Seconds a frame stays retrievable.  No stale-evidence surprise: the reply always
#: carries ``captured_at`` and the fetch always reports ``age_seconds``.
EVIDENCE_MAX_AGE_SECONDS = 3600.0

#: Bounded tombstones for ids a retention policy retired.  WITHOUT this ring the
#: "no longer available" state is indistinguishable from "never existed", and the
#: model cannot tell the two apart — which is the whole point of the four-state
#: distinction the fetch replies make.
EVIDENCE_RETIRED_TOMBSTONES = 64

#: Refusal reasons.  These are the tokens the model reads: ``image_not_stored_reason``
#: on a store refusal, and the ``reasons`` entry of a fetch rejection.
EVIDENCE_REFUSE_TEXT_SESSION = "text_session_no_image"
EVIDENCE_REFUSE_FRAME_TOO_LARGE = "frame_too_large"
EVIDENCE_REFUSE_DISPOSED = "store_disposed"

#: Which retention policy retired a frame — reported back as ``retired_by`` so
#: ``image_expired`` names the policy that retired the id, not just the fact.
RETIRE_COUNT_CAP = "count_cap"
RETIRE_BYTE_CAP = "byte_cap"
RETIRE_AGE_CAP = "age_cap"

#: Prefix + width of a stored image id: 64 bits of ``uuid4().hex``, mirroring
#: ``state._new_id``.  The id is unguessable AND the lookup is session-scoped, so a
#: wrong id is both unpredictable and useless against another session.
_IMAGE_ID_PREFIX = "img_"
_IMAGE_ID_HEX = 16

#: The freshness sentence every fetch ships: the tool says, at the point of use,
#: that a stored frame is a look BACK, never a look forward.
FETCH_FRESHNESS_NOTE = (
    "This is a STORED frame captured earlier in this session and is not necessarily "
    "current. Read captured_at and age_seconds before treating it as present truth; "
    "call computer_observe for what is on screen now. Use this tool to look BACK, "
    "never to look forward."
)

EvidenceOutcome = Literal["found", "expired", "not_found", "disposed"]


def _wall_clock() -> float:
    """The wall clock, read ONLY to stamp the human-readable ``captured_at``.

    It is deliberately a separate, injectable seam from :func:`_monotonic`: mixing a
    wall clock into an expiry decision is the bug this split exists to make
    impossible.
    """
    return time.time()


def _monotonic() -> float:
    """The clock every retention DECISION reads.  Monotonic, like ``limits.py``."""
    return time.monotonic()


def _new_image_id() -> str:
    return _IMAGE_ID_PREFIX + uuid.uuid4().hex[:_IMAGE_ID_HEX]


def _iso_utc(epoch_seconds: float) -> str:
    return (
        datetime.fromtimestamp(epoch_seconds, tz=timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


@dataclass(frozen=True)
class StoredFrame:
    """One retained frame, with everything a reader needs to judge how old it is.

    ``image_base64`` is the **pre-ladder** original: the fetch re-runs the outbound
    ladder at read time, so a frame stored under the budget can be served at full
    resolution and the scale reported on a fetch is the scale of the bytes actually
    being returned.  A stored already-bounded JPEG could never be re-served at full
    resolution, which is why the post-ladder bytes are never what gets kept.
    """

    image_id: str
    session_id: str
    image_base64: str
    byte_size: int
    captured_monotonic: float
    captured_at: str
    width: int
    height: int
    source: str
    observation_id: str | None = None
    digest: str | None = None
    coordinate_scale_x: float | None = None
    coordinate_scale_y: float | None = None
    coordinate_space: str | None = None
    crop_region: tuple[int, int, int, int] | None = None
    visual_view: str | None = None
    redactions_applied: bool = False
    # WP-C3 (ORVEX-2026-109): the pointer position/state AT CAPTURE TIME, in
    # canonical screenshot coordinates. The stored pixels stay CLEAN (the sprite
    # is an outbound-only decoration); ``computer_fetch_image`` composites from
    # these fields onto ITS outbound copy.
    pointer_x: float | None = None
    pointer_y: float | None = None
    pointer_state: str | None = None
    # DELTA 1 (M4): the CANONICAL last_click SNAPSHOT at capture time — the
    # click the frame itself reflects — with ``last_click_age_ms`` measured
    # from the capture, so fetch describes the FRAME (never the ring "now",
    # never physical coordinates).
    last_click_x: float | None = None
    last_click_y: float | None = None
    last_click_button: str | None = None
    last_click_age_ms: float | None = None


@dataclass(frozen=True)
class EvidenceLookup:
    """The answer to one id lookup: found, retired by a policy, or never existed."""

    outcome: EvidenceOutcome
    frame: StoredFrame | None = None
    reason: str | None = None

    @property
    def found(self) -> bool:
        return self.outcome == "found"


class EvidenceStore:
    """A bounded, in-memory, per-session store of captured frames.

    Thread-safe by its own ``threading.Lock`` — NOT by ``bundle.extra``, which is
    unsynchronised and shared with the other session keys.  Every public method takes
    the lock for the whole read-modify-write it performs, so a concurrent teardown
    (which disposes) can never interleave with a store or a lookup and produce a
    half-applied state.
    """

    def __init__(
        self,
        session_id: str,
        *,
        image_delivery: str = "image",
        clock: Callable[[], float] | None = None,
        wall_clock: Callable[[], float] | None = None,
        max_frames: int = EVIDENCE_MAX_FRAMES,
        max_bytes: int = EVIDENCE_MAX_BYTES,
        max_age_seconds: float = EVIDENCE_MAX_AGE_SECONDS,
    ) -> None:
        self.session_id = session_id
        #: Fixed at construction from the session's resolved delivery mode.  A text
        #: session has no image bytes to keep, and the store refuses for the whole
        #: session rather than per payload.
        self.image_delivery = "text" if image_delivery == "text" else "image"
        self._clock = clock or _monotonic
        self._wall_clock = wall_clock or _wall_clock
        self.max_frames = max(1, int(max_frames))
        self.max_bytes = max(1, int(max_bytes))
        self.max_age_seconds = float(max_age_seconds)
        self._lock = threading.Lock()
        self._frames: "OrderedDict[str, StoredFrame]" = OrderedDict()
        self._retired: "OrderedDict[str, str]" = OrderedDict()
        self._bytes = 0
        self._disposed = False

    # --- introspection -------------------------------------------------------------------------

    @property
    def disposed(self) -> bool:
        with self._lock:
            return self._disposed

    @property
    def frame_count(self) -> int:
        with self._lock:
            return len(self._frames)

    @property
    def total_bytes(self) -> int:
        with self._lock:
            return self._bytes

    def retention(self) -> dict[str, Any]:
        """The explicit policy, as the model is shown it."""
        return {
            "max_frames": self.max_frames,
            "max_bytes": self.max_bytes,
            "max_age_seconds": self.max_age_seconds,
        }

    def image_ids(self) -> list[str]:
        """The retained ids, oldest first."""
        with self._lock:
            return list(self._frames)

    def frames(self) -> list[StoredFrame]:
        with self._lock:
            return list(self._frames.values())

    def age_seconds(self, image_id: str) -> float:
        """Monotonic age of one retained frame (0.0 when unknown / retired)."""
        with self._lock:
            frame = self._frames.get(image_id)
            if frame is None:
                return 0.0
            return round(max(0.0, self._clock() - frame.captured_monotonic), 3)

    def captures(self) -> list[dict[str, Any]]:
        """Bounded metadata for every retained frame, oldest first — the store read
        API a plan reply's ``evidence_by_backend`` block is built from.

        Metadata only, never pixels: the handshake block describes what was captured
        (and how to fetch it by id), it does not duplicate bytes that
        ``computer_fetch_image`` already serves.  Works on every backend — the store
        is filled by the reply hooks, not by anything backend-specific.
        """
        with self._lock:
            return [
                {
                    "image_id": frame.image_id,
                    "source": frame.source,
                    "observation_id": frame.observation_id,
                    "captured_at": frame.captured_at,
                    "width": frame.width,
                    "height": frame.height,
                    "byte_size": frame.byte_size,
                    "crop_region": list(frame.crop_region) if frame.crop_region else None,
                    "visual_view": frame.visual_view,
                }
                for frame in self._frames.values()
            ]

    # --- internals (every caller already holds the lock) ---------------------------------------

    def _retire_locked(self, image_id: str, reason: str) -> None:
        frame = self._frames.pop(image_id, None)
        if frame is not None:
            self._bytes -= frame.byte_size
        self._retired[image_id] = reason
        while len(self._retired) > EVIDENCE_RETIRED_TOMBSTONES:
            self._retired.popitem(last=False)

    def _sweep_locked(self) -> None:
        """Drop every frame past the age cap.  Runs on WRITE and on ACCESS, so an
        expired frame is never served and never counted against the caps."""
        now = self._clock()
        for image_id, frame in list(self._frames.items()):
            if (now - frame.captured_monotonic) > self.max_age_seconds:
                self._retire_locked(image_id, RETIRE_AGE_CAP)

    # --- the two public operations -------------------------------------------------------------

    def store(
        self,
        payload: str,
        *,
        source: str,
        attached: bool,
        width: int,
        height: int,
        digest: str | None = None,
        observation_id: str | None = None,
        coordinate_scale_x: float | None = None,
        coordinate_scale_y: float | None = None,
        coordinate_space: str | None = None,
        crop_region: Iterable[int] | None = None,
        visual_view: str | None = None,
        redactions_applied: bool = False,
        pointer_x: float | None = None,
        pointer_y: float | None = None,
        pointer_state: str | None = None,
        last_click: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Retain one captured frame and return the ADDITIVE reply block.

        The block is what makes the four states distinguishable from the reply alone:
        ``image_stored`` + ``image_attached`` + ``image_id`` say whether the frame was
        kept, whether THIS reply carried the pixels, and how to ask for them later.
        A refusal returns the same shape with ``image_stored: false`` and a reason —
        and **no id at all**, so a fetch can never be handed something that was never
        kept.
        """
        with self._lock:
            if self._disposed:
                return _refusal(EVIDENCE_REFUSE_DISPOSED)
            if self.image_delivery == "text" or _payload_is_raw_key(payload):
                # The detector is the belt; the delivery mode is the braces: a backend
                # that rejected raw-payload-key arming hands a text session real PNGs,
                # and serving one as an image block kills the session just as
                # permanently — so BOTH are refused, and neither ever yields an id.
                return _refusal(EVIDENCE_REFUSE_TEXT_SESSION)
            byte_size = _payload_byte_size(payload)
            if byte_size > self.max_bytes:
                return _refusal(EVIDENCE_REFUSE_FRAME_TOO_LARGE)

            self._sweep_locked()
            # FIFO by insertion.  The count cap is applied first, then the byte cap,
            # and each retirement records WHICH policy retired the id, so a later
            # fetch on that id can say so instead of merely "not found".
            while self._frames and len(self._frames) >= self.max_frames:
                self._retire_locked(next(iter(self._frames)), RETIRE_COUNT_CAP)
            while self._frames and self._bytes + byte_size > self.max_bytes:
                self._retire_locked(next(iter(self._frames)), RETIRE_BYTE_CAP)

            captured_monotonic = self._clock()
            image_id = _new_image_id()
            derived = _png_dimensions(payload)
            frame = StoredFrame(
                image_id=image_id,
                session_id=self.session_id,
                image_base64=payload,
                byte_size=byte_size,
                captured_monotonic=captured_monotonic,
                captured_at=_iso_utc(self._wall_clock()),
                width=int(width) if int(width) > 0 else (derived[0] if derived else 0),
                height=int(height) if int(height) > 0 else (derived[1] if derived else 0),
                source=source,
                observation_id=observation_id,
                digest=digest,
                coordinate_scale_x=coordinate_scale_x,
                coordinate_scale_y=coordinate_scale_y,
                coordinate_space=coordinate_space,
                crop_region=tuple(int(v) for v in crop_region) if crop_region else None,
                visual_view=visual_view,
                redactions_applied=redactions_applied,
                pointer_x=pointer_x,
                pointer_y=pointer_y,
                pointer_state=pointer_state,
                last_click_x=last_click.get("x") if isinstance(last_click, dict) else None,
                last_click_y=last_click.get("y") if isinstance(last_click, dict) else None,
                last_click_button=(
                    last_click.get("button") if isinstance(last_click, dict) else None
                ),
                last_click_age_ms=(
                    last_click.get("age_ms") if isinstance(last_click, dict) else None
                ),
            )
            self._frames[image_id] = frame
            self._bytes += byte_size
            return {
                "image_stored": True,
                "image_attached": bool(attached),
                "image_id": image_id,
                "image_captured_at": frame.captured_at,
                "image_age_seconds": 0.0,
                "image_bytes": frame.byte_size,
                "image_width": frame.width,
                "image_height": frame.height,
                "evidence_retention": self.retention(),
                "evidence_source": source,
                "evidence_fetch": (
                    f'computer_fetch_image(session_id, image_id="{image_id}") returns this '
                    "exact frame later in this session; it is kept in memory only and is "
                    "not current unless a later observation says so."
                ),
            }

    def lookup(self, image_id: Any) -> EvidenceLookup:
        """Session-scoped lookup.  A foreign id and a never-existed id are the SAME
        answer (``not_found``) — the store has no cross-session index to leak from,
        and the tool's rejection is byte-identical for both.

        Disposal is observed ATOMICALLY (VAL-WP5 F3): the ``_disposed`` check shares
        the same lock as the frame read, so a fetch racing the teardown answers
        ``disposed`` — never ``not_found`` for a frame that existed a moment ago.
        """
        if not isinstance(image_id, str) or not image_id:
            return EvidenceLookup("not_found")
        with self._lock:
            if self._disposed:
                return EvidenceLookup("disposed", reason=EVIDENCE_REFUSE_DISPOSED)
            frame = self._frames.get(image_id)
            if frame is not None:
                age = self._clock() - frame.captured_monotonic
                if age > self.max_age_seconds:
                    self._retire_locked(image_id, RETIRE_AGE_CAP)
                    return EvidenceLookup("expired", reason=RETIRE_AGE_CAP)
                return EvidenceLookup("found", frame=frame)
            if image_id in self._retired:
                return EvidenceLookup("expired", reason=self._retired[image_id])
            return EvidenceLookup("not_found")

    def dispose(self) -> int:
        """Empty the store for good and report how many frames it held.

        Called from the ONE session-teardown path, before the bundle is popped.  A
        store that a suspended tool call still holds a reference to answers every
        later call as disposed, so a resurrection can never repopulate it.
        """
        with self._lock:
            released = len(self._frames)
            self._frames.clear()
            self._retired.clear()
            self._bytes = 0
            self._disposed = True
            return released


def _payload_byte_size(payload: str) -> int:
    """The price of a payload against the byte cap: ``len`` of the base64 ``str``
    that is actually HELD in memory (VAL-WP5 F4 — the decoded size under-priced
    residency by ~33%).  The cap therefore bounds resident memory directly, and this
    needs no decode at all (the old full-payload decode ran under the store lock)."""
    return len(payload)


#: The 8-byte PNG signature and the byte offsets of the IHDR width/height fields.
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_PNG_IHDR_WIDTH_OFFSET = 16
_PNG_IHDR_HEIGHT_OFFSET = 20
_PNG_HEADER_BYTES = 24


def _png_dimensions(payload: str) -> tuple[int, int] | None:
    """Read a PNG's pixel dimensions from its IHDR, without decoding any pixel.

    Stdlib only, and it decodes ONLY the first 32 base64 characters (the 24 header
    bytes) — never the whole payload (VAL-WP5 F9: the old full decode ran under the
    store lock for 24 bytes of answer).  The execute path has no ``Observation`` in
    hand — ``ExecutionResult`` carries only the base64 blob — so deriving the size
    from the bytes themselves is the honest way to report a stored frame's dimensions
    instead of inventing ``0``.
    """
    try:
        header = base64.b64decode(payload[:32], validate=True)
    except Exception:  # noqa: BLE001 - a non-PNG payload simply has no readable header
        return None
    if len(header) < _PNG_HEADER_BYTES or not header.startswith(_PNG_SIGNATURE):
        return None
    width = int.from_bytes(header[_PNG_IHDR_WIDTH_OFFSET : _PNG_IHDR_WIDTH_OFFSET + 4], "big")
    height = int.from_bytes(header[_PNG_IHDR_HEIGHT_OFFSET : _PNG_IHDR_HEIGHT_OFFSET + 4], "big")
    if width <= 0 or height <= 0:
        return None
    return width, height


def _refusal(reason: str) -> dict[str, Any]:
    """The refusal block: honest, uniform, and carrying NO id.

    ``image_attached`` is unconditionally ``False`` here: a refusal means there were
    no image bytes to attach, whatever the caller asked for, so saying ``True`` would
    be a lie the model could act on.
    """
    return {
        "image_stored": False,
        "image_attached": False,
        "image_not_stored_reason": reason,
    }


# --- the server seam ------------------------------------------------------------------------
# These four functions are the ONLY way ``server.py`` touches the store.  Each one
# reads the key and never creates it, so a hook that runs after a teardown writes
# nothing instead of resurrecting a store on a dead bundle.


def new_store(session_id: str, *, image_delivery: str = "image") -> EvidenceStore:
    """Build the store for a NEW session.  Called once, at bundle construction."""
    return EvidenceStore(session_id, image_delivery=image_delivery)


def evidence_store(extra: dict[str, Any]) -> EvidenceStore | None:
    """The session's store, or ``None`` when there is none (never creates one)."""
    store = extra.get(EVIDENCE_STORE_KEY)
    return store if isinstance(store, EvidenceStore) else None


def dispose_store(extra: dict[str, Any]) -> int:
    """Pop the key and empty the store.  The single removal site (session teardown);
    a second call finds no key and releases nothing — disposal is exactly-once."""
    store = extra.pop(EVIDENCE_STORE_KEY, None)
    if isinstance(store, EvidenceStore):
        return store.dispose()
    return 0


def register_frame(
    extra: dict[str, Any],
    *,
    payload: str | None,
    source: str,
    attached: bool,
    width: int = 0,
    height: int = 0,
    digest: str | None = None,
    observation_id: str | None = None,
    coordinate_scale_x: float | None = None,
    coordinate_scale_y: float | None = None,
    coordinate_space: str | None = None,
    crop_region: Iterable[int] | None = None,
    visual_view: str | None = None,
    redactions_applied: bool = False,
    pointer_x: float | None = None,
    pointer_y: float | None = None,
    pointer_state: str | None = None,
    last_click: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Retain one frame and return the additive reply block (``{}`` when there is
    nothing to do: no payload means no capture, and a missing store means the session
    is already gone — both are cases where adding keys would be a lie).

    N2 normalization (VAL-WP5-02, fixed in WP7a DELTA 1): THIS is the single storage
    point for ``visual_view`` — the raw/pass-through convention is ``None``
    everywhere, so a caller whose explicit view selector is the literal ``"raw"``
    (observe/zoom/execute all accept it) stores ``None``, never the string. A
    derived view keeps its id (``"grid"``, ...), so a fetched frame is
    self-describing about derivations only.

    WP-C3: ``pointer_x``/``pointer_y``/``pointer_state`` record the pointer AT
    CAPTURE TIME (canonical screenshot coordinates); the payload itself stays clean.
    DELTA 1 (M4): ``last_click`` is the canonical click snapshot ({x, y, button,
    age_ms} at capture) stored alongside so fetch describes the frame.
    """
    store = evidence_store(extra)
    if store is None or not payload:
        return {}
    return store.store(
        payload,
        source=source,
        attached=attached,
        width=width,
        height=height,
        digest=digest,
        observation_id=observation_id,
        coordinate_scale_x=coordinate_scale_x,
        coordinate_scale_y=coordinate_scale_y,
        coordinate_space=coordinate_space,
        crop_region=crop_region,
        visual_view=None if visual_view == "raw" else visual_view,
        redactions_applied=redactions_applied,
        pointer_x=pointer_x,
        pointer_y=pointer_y,
        pointer_state=pointer_state,
        last_click=last_click,
    )


# --- WP-R (ORVEX-2026-109): the SEPARATE recording pool ---------------------------------------
# Recording keyframes live in their OWN bounded per-session pool so a recording
# can NEVER evict an observation frame (and vice versa): 24 frames / 16 MB /
# 3600 s, the entries of at most the LAST 2 recordings per session.  The held
# MP4 bytes (``include_video``) are priced against the same byte cap — they are
# resident memory exactly like the frames.  The lifecycle shape is the
# observation store's: the key is written once at bundle construction, every
# accessor only READS it, and :func:`dispose_recording_store` is the single
# removal site (session teardown).

#: The ``bundle.extra`` key the recording pool lives under.
RECORDING_STORE_KEY = "recording_store"

#: Keyframes retained per session (across the last recordings).
RECORDING_MAX_FRAMES = 24

#: Bytes retained per session — keyframe base64 AND held MP4 bytes together.
RECORDING_MAX_BYTES = 16 * 1024 * 1024

#: Seconds recording entries stay retrievable.
RECORDING_MAX_AGE_SECONDS = 3600.0

#: Entries from the last N completed recordings per session are kept; older
#: recordings' entries are retired wholesale on each new completion.
RECORDING_KEEP_RECORDINGS = 2


@dataclass(frozen=True)
class RecordingEntry:
    """One retained recording-pool entry.

    ``kind`` is ``"keyframe"`` (a JPEG frame, fetchable by ``image_id`` through
    ``computer_fetch_image`` exactly like an observation frame) or ``"video"``
    (the held MP4, retrievable later by ``record_id`` while retained).
    """

    entry_id: str
    kind: str  # "keyframe" | "video"
    session_id: str
    record_id: str
    image_base64: str | None  # keyframe JPEG, base64
    video_bytes: bytes | None  # MP4
    byte_size: int
    captured_monotonic: float
    captured_at: str
    width: int
    height: int
    t_ms: float = 0.0
    pointer_x: float | None = None
    pointer_y: float | None = None
    pointer_state: str | None = None
    fps: int | None = None
    mime: str = "image/jpeg"
    uri: str | None = None


class RecordingEvidenceStore:
    """The bounded, in-memory, per-session recording pool (own lock)."""

    def __init__(
        self,
        session_id: str,
        *,
        clock: Callable[[], float] | None = None,
        wall_clock: Callable[[], float] | None = None,
        max_frames: int = RECORDING_MAX_FRAMES,
        max_bytes: int = RECORDING_MAX_BYTES,
        max_age_seconds: float = RECORDING_MAX_AGE_SECONDS,
        keep_recordings: int = RECORDING_KEEP_RECORDINGS,
    ) -> None:
        self.session_id = session_id
        self._clock = clock or _monotonic
        self._wall_clock = wall_clock or _wall_clock
        self.max_frames = max(1, int(max_frames))
        self.max_bytes = max(1, int(max_bytes))
        self.max_age_seconds = float(max_age_seconds)
        self.keep_recordings = max(1, int(keep_recordings))
        self._lock = threading.Lock()
        self._entries: "OrderedDict[str, RecordingEntry]" = OrderedDict()
        self._retired: "OrderedDict[str, str]" = OrderedDict()
        # DELTA 2 N1: per-keyframe frame metadata — entry_id -> (scale, origin)
        # where scale is FRAME px per CANONICAL px and origin is the canonical
        # region origin.  Published as `frame_to_canonical` (the INVERSE scale
        # — canonical px per frame px — plus the origin); the inverted
        # `coordinate_scale_*` is deliberately OMITTED for recording frames.
        self._frame_meta: dict[str, tuple[float, tuple[int, int]]] = {}
        # DELTA 3 BLOCKER: held-frames entry id -> weakref to the owning
        # Recording; eviction frees the Recording's JPEGs through it.
        self._frames_owners: dict[str, Any] = {}
        self._bytes = 0
        self._disposed = False

    # --- introspection ----------------------------------------------------------------------

    @property
    def disposed(self) -> bool:
        with self._lock:
            return self._disposed

    @property
    def max_bytes(self) -> int:  # noqa: A003 - mirrors EvidenceStore's shape
        return self._max_bytes

    @max_bytes.setter
    def max_bytes(self, value: int) -> None:
        self._max_bytes = max(1, int(value))

    def entry_count(self) -> int:
        with self._lock:
            return len(self._entries)

    def total_bytes(self) -> int:
        with self._lock:
            return self._bytes

    def age_seconds(self, entry_id: str) -> float:
        """Monotonic age of one retained entry (0.0 when unknown / retired)."""
        with self._lock:
            entry = self._entries.get(entry_id)
            if entry is None:
                return 0.0
            return round(max(0.0, self._clock() - entry.captured_monotonic), 3)

    def retention(self) -> dict[str, Any]:
        return {
            "max_frames": self.max_frames,
            "max_bytes": self._max_bytes,
            "max_age_seconds": self.max_age_seconds,
            "keep_recordings": self.keep_recordings,
        }

    # --- internals --------------------------------------------------------------------------

    def _retire_locked(self, entry_id: str, reason: str) -> None:
        entry = self._entries.pop(entry_id, None)
        if entry is not None:
            self._bytes -= entry.byte_size
            # DELTA 3 BLOCKER: when a held-frames entry is evicted, FREE the
            # Recording's JPEGs via the registered owner callback — no phantom
            # memory outside the cap.
            if entry.kind == "frames":
                owner = self._frames_owners.pop(entry_id, None)
                if owner is not None:
                    recording = owner()
                    if recording is not None:
                        try:
                            recording.release_frame_payloads()
                        except Exception:  # noqa: BLE001 - never break eviction
                            pass
        self._frame_meta.pop(entry_id, None)
        self._retired[entry_id] = reason
        while len(self._retired) > EVIDENCE_RETIRED_TOMBSTONES:
            self._retired.popitem(last=False)

    def _sweep_locked(self) -> None:
        now = self._clock()
        for entry_id, entry in list(self._entries.items()):
            if (now - entry.captured_monotonic) > self.max_age_seconds:
                self._retire_locked(entry_id, RETIRE_AGE_CAP)

    def _evict_frames_entries_locked(self, *, exclude_record_id: str | None = None) -> None:
        """DELTA 3 BLOCKER: held-frames entries are the FIRST thing evicted
        under pressure — dropped (oldest first) before any keyframe, video or
        observation frame.  Each drop FREES the owning Recording's JPEGs."""
        frames_ids = [
            entry_id
            for entry_id, entry in self._entries.items()
            if entry.kind == "frames"
            and (exclude_record_id is None or entry.record_id != exclude_record_id)
        ]
        for entry_id in frames_ids:
            self._retire_locked(entry_id, RETIRE_BYTE_CAP)

    def _evict_older_recordings_locked(self, record_id: str) -> None:
        """Keep only the last ``keep_recordings`` recordings' entries."""
        record_ids: list[str] = []
        for entry in self._entries.values():
            if entry.record_id not in record_ids:
                record_ids.append(entry.record_id)
        while len(record_ids) > self.keep_recordings:
            oldest = record_ids.pop(0)
            if oldest == record_id:
                continue
            for entry_id, entry in list(self._entries.items()):
                if entry.record_id == oldest:
                    self._retire_locked(entry_id, RETIRE_COUNT_CAP)

    def _oldest_frames_entry_locked(self) -> str | None:
        """DELTA 4 F2: the OLDEST ``kind=="frames"`` entry, or None."""
        for entry_id, entry in self._entries.items():
            if entry.kind == "frames":
                return entry_id
        return None

    def _make_room_locked(self, byte_size: int) -> bool:
        """Evict until the new byte size fits; False when hopeless.

        DELTA 3 BLOCKER + DELTA 4 F2: held-frames entries are evicted FIRST
        (oldest frames first, each drop freeing the owning Recording's
        JPEGs) — but ONLY while the pool is actually OVER one of its caps
        (the incoming store would not fit, or the count cap is full).  A
        store that fits never evicts a frames entry."""
        while (
            self._oldest_frames_entry_locked() is not None
            and (
                len(self._entries) >= self.max_frames
                or self._bytes + byte_size > self._max_bytes
            )
        ):
            oldest_frames = self._oldest_frames_entry_locked()
            if oldest_frames is None:
                break
            self._retire_locked(oldest_frames, RETIRE_BYTE_CAP)
        while self._entries and len(self._entries) >= self.max_frames:
            self._retire_locked(next(iter(self._entries)), RETIRE_COUNT_CAP)
        while self._entries and self._bytes + byte_size > self._max_bytes:
            self._retire_locked(next(iter(self._entries)), RETIRE_BYTE_CAP)
        return self._bytes + byte_size <= self._max_bytes

    # --- the two public operations ----------------------------------------------------------

    def store_keyframe(
        self,
        *,
        record_id: str,
        jpeg_base64: str,
        width: int,
        height: int,
        t_ms: float,
        pointer_x: float | None,
        pointer_y: float | None,
        pointer_state: str | None,
        frame_scale: float | None = None,
        origin: tuple[int, int] = (0, 0),
    ) -> str | None:
        """Retain one recording keyframe; return its ``img_`` id (None when
        disposed or refused — the reply simply carries no id for that frame).

        DELTA 1 M2/DELTA 2 N1: ``pointer_x``/``pointer_y`` arrive in FRAME
        space (the frame's own pixels — fetch composites at exactly those
        pixels); ``frame_scale`` (FRAME px per CANONICAL px) and ``origin``
        (the canonical region origin) are stored as the keyframe's
        ``frame_to_canonical`` metadata — the fetch publishes
        ``{"scale": 1/frame_scale, "origin": [x, y]}`` and the pointer block
        in CANONICAL coordinates (never the inverted ``coordinate_scale_*``)."""
        byte_size = len(jpeg_base64)
        with self._lock:
            if self._disposed:
                return None
            if byte_size > self._max_bytes:
                return None
            self._evict_older_recordings_locked(record_id)
            if not self._make_room_locked(byte_size):
                return None
            entry = RecordingEntry(
                entry_id=_new_image_id(),
                kind="keyframe",
                session_id=self.session_id,
                record_id=record_id,
                image_base64=jpeg_base64,
                video_bytes=None,
                byte_size=byte_size,
                captured_monotonic=self._clock(),
                captured_at=_iso_utc(self._wall_clock()),
                width=width,
                height=height,
                t_ms=t_ms,
                pointer_x=pointer_x,
                pointer_y=pointer_y,
                pointer_state=pointer_state,
                mime="image/jpeg",
                uri=None,
            )
            self._entries[entry.entry_id] = entry
            self._bytes += byte_size
            self._frame_meta[entry.entry_id] = (
                float(frame_scale) if frame_scale else 1.0,
                (int(origin[0]), int(origin[1])),
            )
            return entry.entry_id

    def frame_to_canonical(self, image_id: str) -> dict[str, Any] | None:
        """DELTA 2 N1: the keyframe's frame→canonical mapping —
        ``{"scale": <canonical px per frame px>, "origin": [x, y]}`` — or
        None for unknown ids and non-recording frames.  A keyframe pixel
        maps back with ``canonical = origin + pixel × scale``."""
        with self._lock:
            meta = self._frame_meta.get(image_id)
        if meta is None:
            return None
        scale, origin = meta
        effective = scale if scale > 0 else 1.0
        return {"scale": round(1.0 / effective, 6), "origin": [origin[0], origin[1]]}

    def store_frames(self, *, record_id: str, total_bytes: int, owner: Any = None) -> bool:
        """DELTA 2 N3 / DELTA 3 BLOCKER: account for the recording's held
        frame JPEGs under the SAME byte cap (the Recording keeps the bytes;
        this entry is the accounting + eviction marker, with ``owner`` a
        WEAK-reference-able callback target whose JPEGs are FREED when the
        entry is evicted — no phantom memory).

        Eviction priority: OTHER recordings' held-frames entries are dropped
        first (oldest first, each freeing its Recording); THIS recording's
        keyframes and video are NEVER evicted to make room for its frames —
        if they still do not fit, the entry is REFUSED (False) and the caller
        releases the JPEGs at once and marks the recording
        ``frames_released`` (``frames_accounted`` stays False — honest)."""
        with self._lock:
            if self._disposed or total_bytes <= 0 or total_bytes > self._max_bytes:
                return False
            entry_id = f"{record_id}:frames"
            old = self._entries.pop(entry_id, None)
            if old is not None:
                self._bytes -= old.byte_size
                self._frames_owners.pop(entry_id, None)
            # DELTA 4 F2: held frames of OTHER recordings die first ONLY
            # while the pool is over one of its caps (this store would not
            # fit, or the count cap is full) — never merely because a store
            # happened.  Each drop frees its Recording's JPEGs; THIS
            # recording's keyframes and video are never touched for its
            # frames.
            over_caps = (
                len(self._entries) >= self.max_frames
                or self._bytes + total_bytes > self._max_bytes
            )
            if over_caps:
                self._evict_frames_entries_locked(exclude_record_id=record_id)
            self._evict_older_recordings_locked(record_id)
            if self._bytes + total_bytes > self._max_bytes:
                return False
            if len(self._entries) >= self.max_frames and entry_id not in self._entries:
                return False
            entry = RecordingEntry(
                entry_id=entry_id,
                kind="frames",
                session_id=self.session_id,
                record_id=record_id,
                image_base64=None,
                video_bytes=None,
                byte_size=total_bytes,
                captured_monotonic=self._clock(),
                captured_at=_iso_utc(self._wall_clock()),
                width=0,
                height=0,
                mime="application/octet-stream",
                uri=None,
            )
            self._entries[entry_id] = entry
            self._bytes += total_bytes
            try:
                self._frames_owners[entry_id] = weakref.ref(owner) if owner is not None else None
            except TypeError:
                self._frames_owners[entry_id] = None
            return True

    def remove_frames(self, record_id: str) -> bool:
        """DELTA 3 BLOCKER: drop the recording's held-frames accounting entry
        — called whenever the payloads are RELEASED (for example after a
        successful fallback encode), so the pool's byte count always matches
        real held memory."""
        with self._lock:
            entry = self._entries.pop(f"{record_id}:frames", None)
            if entry is None:
                return False
            self._bytes -= entry.byte_size
            self._frames_owners.pop(entry.entry_id, None)
            return True

    def has_frames(self, record_id: str) -> bool:
        """DELTA 2 N3: the recording's held frames are STILL accounted (not
        evicted).  False ⇒ the frames are gone (released)."""
        with self._lock:
            if self._disposed:
                return False
            entry = self._entries.get(f"{record_id}:frames")
            return entry is not None and entry.kind == "frames"

    def store_video(
        self,
        *,
        record_id: str,
        mp4_bytes: bytes,
        width: int,
        height: int,
        fps: int,
        frames: int,
    ) -> bool:
        """Retain the held MP4; best-effort.

        DELTA 1: the entry is priced exactly like a keyframe — the length of
        the base64 string the blob travels as (the NIT: pricing is consistent
        across both entry kinds) — and REPLACING an existing video for the
        same record_id subtracts the old entry's size first (no phantom
        bytes, no premature eviction)."""
        with self._lock:
            if self._disposed or len(mp4_bytes) > self._max_bytes:
                return False
            # Replace accounting: the old entry's bytes leave the pool before
            # the new ones are counted (DELTA 1 M5).
            old = self._entries.pop(record_id, None)
            if old is not None:
                self._bytes -= old.byte_size
                self._frame_meta.pop(record_id, None)
            blob_b64 = base64.b64encode(mp4_bytes).decode("ascii")
            byte_size = len(blob_b64)
            self._evict_older_recordings_locked(record_id)
            if not self._make_room_locked(byte_size):
                return False
            entry = RecordingEntry(
                entry_id=record_id,
                kind="video",
                session_id=self.session_id,
                record_id=record_id,
                image_base64=None,
                video_bytes=mp4_bytes,
                byte_size=byte_size,
                captured_monotonic=self._clock(),
                captured_at=_iso_utc(self._wall_clock()),
                width=width,
                height=height,
                fps=fps,
                mime="video/mp4",
                uri=f"cortex://recording/{record_id}.mp4",
            )
            self._entries[record_id] = entry
            self._bytes += byte_size
            return True

    def lookup(self, image_id: Any) -> EvidenceLookup:
        """Session-scoped keyframe lookup (the EvidenceStore answer shape, so
        ``computer_fetch_image`` can consult both pools through one code path)."""
        if not isinstance(image_id, str) or not image_id:
            return EvidenceLookup("not_found")
        with self._lock:
            if self._disposed:
                return EvidenceLookup("disposed", reason=EVIDENCE_REFUSE_DISPOSED)
            entry = self._entries.get(image_id)
            if entry is not None and entry.kind == "keyframe":
                age = self._clock() - entry.captured_monotonic
                if age > self.max_age_seconds:
                    self._retire_locked(image_id, RETIRE_AGE_CAP)
                    return EvidenceLookup("expired", reason=RETIRE_AGE_CAP)
                frame = StoredFrame(
                    image_id=entry.entry_id,
                    session_id=self.session_id,
                    image_base64=entry.image_base64 or "",
                    byte_size=entry.byte_size,
                    captured_monotonic=entry.captured_monotonic,
                    captured_at=entry.captured_at,
                    width=entry.width,
                    height=entry.height,
                    source="computer_record",
                    # DELTA 1 M2: the pointer is stored in FRAME space (the
                    # frame's own pixels — fetch composites at scale 1.0 on
                    # exactly those pixels); the frame↔canonical scale rides
                    # the coordinate metadata so a consumer can convert.
                    # DELTA 2 N1: the inverted coordinate_scale_* is OMITTED
                    # for recording frames — the direction (frame px per
                    # canonical px) is the OPPOSITE of the field's documented
                    # meaning elsewhere; the mapping is published as
                    # `frame_to_canonical` instead.
                    coordinate_scale_x=None,
                    coordinate_scale_y=None,
                    coordinate_space="recording_frame",
                    pointer_x=entry.pointer_x,
                    pointer_y=entry.pointer_y,
                    pointer_state=entry.pointer_state,
                )
                return EvidenceLookup("found", frame=frame)
            if image_id in self._retired:
                return EvidenceLookup("expired", reason=self._retired[image_id])
            return EvidenceLookup("not_found")

    def lookup_video(self, record_id: str) -> RecordingEntry | None:
        """The retained MP4 for one record_id, or None (expired/never held)."""
        with self._lock:
            if self._disposed:
                return None
            self._sweep_locked()
            entry = self._entries.get(record_id)
            if entry is not None and entry.kind == "video":
                return entry
            return None

    def dispose(self) -> int:
        with self._lock:
            released = len(self._entries)
            self._entries.clear()
            self._retired.clear()
            self._frame_meta.clear()
            self._frames_owners.clear()
            self._bytes = 0
            self._disposed = True
            return released


def new_recording_store(session_id: str) -> RecordingEvidenceStore:
    """Build the recording pool for a NEW session (once, at bundle construction)."""
    return RecordingEvidenceStore(session_id)


def recording_store(extra: dict[str, Any]) -> RecordingEvidenceStore | None:
    """The session's recording pool, or None when there is none (never creates)."""
    store = extra.get(RECORDING_STORE_KEY)
    return store if isinstance(store, RecordingEvidenceStore) else None


def dispose_recording_store(extra: dict[str, Any]) -> int:
    """Pop the key and empty the pool (the single removal site, session teardown)."""
    store = extra.pop(RECORDING_STORE_KEY, None)
    if isinstance(store, RecordingEvidenceStore):
        return store.dispose()
    return 0
