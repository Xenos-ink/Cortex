"""UIA semantic reader, the Win32 fallback reader, and snapshot-to-field converters."""

from __future__ import annotations

import ctypes
import logging
import queue
import threading
import time
import uuid
from collections.abc import Callable

from ..core.models import TextRegion
from .client_budget import semantic_read_timeout_seconds, semantic_warm_timeout_seconds

logger = logging.getLogger(__name__)
from .win32 import (
    IS_WINDOWS,
    _CLSCTX_INPROC_SERVER,
    _COINIT_APARTMENTTHREADED,
    _GUID,
    _GUITHREADINFO,
    _GW_CHILD,
    _GW_HWNDNEXT,
    _RPC_E_CHANGED_MODE,
    _SENDMESSAGE_TIMEOUT_MS,
    _SMTO_ABORTIFHUNG,
    _TREE_SCOPE_CHILDREN,
    _VARIANT,
    _VT_ARRAY,
    _VT_BOOL,
    _VT_BSTR,
    _VT_EMPTY,
    _VT_I4,
    _VT_R8,
    _WM_GETTEXT,
    _ole32,
    _oleaut32,
    _user32,
)

UIA_READ_ENV = "CORTEX_UIA_READ"  # "0" disables the UIA semantic read (default on)
#: Control text via WM_GETTEXT is read with ONE fixed-buffer call (see
#: ``_window_text_via_message``): the buffer must span the WHOLE control text because
#: WM_GETTEXT returns text from the START — the R-04 integrity verifier compares the
#: buffer's TAIL against the typed text.
_WINDOW_TEXT_MAX_CHARS = 8192


class _SingleOwnerComWorker:
    """ONE long-lived daemon thread owns COM for one UIA reader's whole life.

    WP13 DELTA-2 BLOCKER fix (replaces the DELTA-1 per-call thread harness):
    CoInitializeEx(APARTMENTTHREADED) + CoCreateInstance run on the OWNER
    thread, and EVERY UIA call (warm, read, element summaries, property reads)
    is submitted to and executed ONLY there — the raw ``IUIAutomation`` pointer
    is never touched from any other thread, and the STA that owns the object
    never dies while the reader lives.

    Callers submit a request and wait at most their wall
    (:func:`~computer_use_mcp.desktop.client_budget.semantic_read_timeout_seconds`).
    While a request is still in flight past its wall (a hung WebView2/
    Chromium provider), new requests are NOT queued behind it: they return
    ``(None, timed_out=True)`` AT ONCE (busy/degraded; the gate is an atomic
    ``Lock.acquire(blocking=False)`` — WP13 DELTA-3 n1). There is never more
    than one worker per reader, so hung calls can never pile up threads or run
    concurrently on one STA object. When the hung call finally returns, the
    worker drops the stale result (its consumer is gone) and serves new
    requests again — recovery without restart.

    Shutdown (WP13 DELTA-3 m3): :meth:`stop` queues a sentinel request; the
    owner then runs ``on_stop`` (release the CUIAutomation pointer ON the owner
    thread) and ``CoUninitialize`` before exiting — no leaked COM object or
    thread per session.
    """

    _STOP = object()  # sentinel request: stop the owner thread

    def __init__(self, on_stop: Callable[[], None] | None = None) -> None:
        self._queue: queue.SimpleQueue = queue.SimpleQueue()
        # n1 (VAL-WP13-01): the busy gate is an atomic non-blocking acquire —
        # check-then-set on an Event was not atomic under concurrent callers.
        self._busy_lock = threading.Lock()
        self._on_stop = on_stop
        self._thread = threading.Thread(
            target=self._serve, daemon=True, name="cortex-uia-com-owner"
        )
        self._thread.start()

    @property
    def owner_ident(self) -> int:
        """The OWNER thread's ident (every UIA call runs on it — test seam)."""
        return self._thread.ident or 0

    def stop(self) -> None:
        """Queue the stop sentinel; the owner releases COM and exits. Never
        blocks (the sentinel result is nobody's business) and is idempotent."""
        self._queue.put((self._STOP, threading.Event(), {"value": None, "done": False}))

    def _serve(self) -> None:
        while True:
            fn, done, outcome = self._queue.get()
            if fn is self._STOP:
                if self._on_stop is not None:
                    try:
                        self._on_stop()
                    except Exception:  # noqa: BLE001 - shutdown is best-effort
                        pass
                break
            try:
                try:
                    outcome["value"] = fn()
                except Exception:  # noqa: BLE001 - the read path degrades to None on any failure
                    outcome["value"] = None
                except SystemExit:
                    raise
                except KeyboardInterrupt:
                    raise
                except BaseException:
                    # n3 (VAL-WP13-02): the owner must survive a stray
                    # BaseException (a killed request must not silence the
                    # reader for the rest of the process) — log at debug and
                    # continue; SystemExit/KeyboardInterrupt re-raise above.
                    logger.debug("UIA owner request raised BaseException", exc_info=True)
                    outcome["value"] = None
            finally:
                outcome["done"] = True
                done.set()
                # A caller whose wall expired has ALREADY returned None; the
                # stale result is dropped here (no late consumer exists) and
                # the worker is free for new requests again.
                self._busy_lock.release()

    def run_bounded(self, fn: Callable[[], object], timeout_s: float) -> tuple[object, bool]:
        """Run ``fn`` on the OWNER thread; ``(result, timed_out)``.

        ``timed_out`` is True when the wall expired OR the worker is still busy
        with a previous (hung) request — in both cases the caller's result is
        ``None`` and the worker state is untouched. Calling this FROM the owner
        thread is a programming error (M2, VAL-WP13-01): the busy gate would
        swallow the call — it raises RuntimeError instead (tests fail loudly;
        production readers call the ``_*_unbounded`` bodies directly and the
        read-path callers degrade via their own ``except``).
        """
        if threading.get_ident() == self._thread.ident:
            raise RuntimeError(
                "run_bounded re-entered on the COM owner thread — call the "
                "_*_unbounded body directly instead"
            )
        if not self._busy_lock.acquire(blocking=False):
            return None, True  # busy: never queued behind a hang, never a second worker
        done = threading.Event()
        outcome: dict[str, object] = {"value": None, "done": False}
        self._queue.put((fn, done, outcome))
        if not done.wait(timeout_s):
            return None, True  # still in flight: the caller degrades, the owner keeps working
        return outcome["value"], False

# --- UIA semantic read bounds (PERF-004; research digest Q3) --------------------------------
UIA_MAX_ELEMENTS = 30  # bounded element list (visible buttons/fields)
UIA_MAX_DEPTH = 2  # focused element + its direct children
UIA_READ_BUDGET_SECONDS = 0.05  # hard wall for one semantic read; past it: partial

# --- D15 (ORVEX-2026-108 WP4b): the semantic read REPORTS what it dropped --------------------
#: Snapshot key carrying the per-read drop breakdown. A reader that finds N elements and
#: hands over fewer must say so: ``omitted_count`` in the ``spatial_text`` block is
#: computed at the CONSUMER boundary, which structurally cannot see the producer's own
#: caps — that is how 33 of 45 known regions shipped as ``region_count=12,
#: omitted_count=0`` (a healthy-looking block for a partial read).
SNAPSHOT_DROPPED_KEY = "dropped"

#: The drop reasons a reader may report. Every one of them is a KNOWN loss, never a
#: guess: ``cap_exceeded`` (the element budget stopped the walk), ``budget_exceeded``
#: (the wall-clock budget did), ``offscreen`` (deliberately not a visible region),
#: ``unavailable`` (an element the walk could not summarize).
SNAPSHOT_DROP_REASONS: tuple[str, ...] = (
    "cap_exceeded",
    "budget_exceeded",
    "offscreen",
    "unavailable",
)


def _empty_drops() -> dict[str, int]:
    return {reason: 0 for reason in SNAPSHOT_DROP_REASONS}


def snapshot_dropped_total(dropped: object) -> int:
    """Total known-but-unserved elements in a snapshot's drop breakdown (0 when absent).

    Defensive by construction: an untrusted / hand-built snapshot's breakdown is
    coerced to ints, unknown keys are ignored, and anything unusable contributes
    nothing — an unreadable report must never inflate ``omitted_count`` with a guess.
    """
    if not isinstance(dropped, dict):
        return 0
    total = 0
    for reason in SNAPSHOT_DROP_REASONS:
        value = dropped.get(reason)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if value > 0:
            total += int(value)
    return total

# UIA property IDs (UIAutomationClient.h)
_UIA_PROP_BOUNDING_RECTANGLE = 30001
_UIA_PROP_CONTROL_TYPE = 30003
_UIA_PROP_NAME = 30005
_UIA_PROP_AUTOMATION_ID = 30011
_UIA_PROP_VALUE = 30045
_UIA_PROP_IS_OFFSCREEN = 30022

# CLSID/IID for the raw-ctypes UIA COM client (UIAutomationClient.dll)
_CLSID_CUIAUTOMATION = "ff48dba4-60ef-4201-aa87-54103eef594e"
_IID_IUIAUTOMATION = "30cbe57d-d9d3-4eac-bca0-31ca10efc51e"

# IUIAutomation / IUIAutomationElement / IUIAutomationElementArray vtable slots
# (IUnknown occupies 0-2; order fixed by UIAutomationClient.h).
_UIA_GET_FOCUSED_ELEMENT = 7
_UIA_CREATE_TRUE_CONDITION = 22
_UIA_ELEMENT_FIND_ALL = 6
_UIA_ELEMENT_GET_PROPERTY_VALUE = 10
_UIA_ARRAY_GET_LENGTH = 3
_UIA_ARRAY_GET_ELEMENT = 4
_COM_RELEASE = 2

UIA_CONTROLTYPE_NAMES: dict[int, str] = {
    50000: "Button",
    50001: "Calendar",
    50002: "CheckBox",
    50003: "ComboBox",
    50004: "Edit",
    50005: "Hyperlink",
    50006: "Image",
    50007: "ListItem",
    50008: "List",
    50009: "Menu",
    50010: "MenuBar",
    50011: "MenuItem",
    50012: "ProgressBar",
    50013: "RadioButton",
    50014: "ScrollBar",
    50015: "Slider",
    50016: "Spinner",
    50017: "StatusBar",
    50018: "Tab",
    50019: "TabItem",
    50020: "Text",
    50021: "ToolBar",
    50022: "ToolTip",
    50023: "Tree",
    50024: "TreeItem",
    50025: "Custom",
    50026: "Group",
    50027: "Thumb",
    50028: "DataGrid",
    50029: "DataItem",
    50030: "Document",
    50031: "SplitButton",
    50032: "Window",
    50033: "Pane",
    50034: "Header",
    50035: "HeaderItem",
    50036: "Table",
    50037: "TitleBar",
    50038: "Separator",
}


# --- UIA semantic read (PERF-004): raw ctypes COM, zero dependencies --------------------------


def _guid_from_string(value: str) -> _GUID:
    """Windows ``GUID`` struct from its canonical string form (little-endian layout)."""
    return _GUID.from_buffer_copy(uuid.UUID(value).bytes_le)


def _com_call(pointer: int, index: int, argtypes: tuple[type, ...], *args: object) -> int:
    """Call method ``index`` on a COM interface pointer (stdcall; returns HRESULT).

    Failed HRESULTs (negative) are RETURNED, not raised: property getters legitimately
    fail per-property (unsupported value patterns) and the snapshot degrades per field.
    Unrecoverable plumbing failures (bad vtable access) raise and are caught by callers.
    """
    vtable_ptr = ctypes.cast(ctypes.c_void_p(pointer), ctypes.POINTER(ctypes.c_void_p)).contents
    function_addr = ctypes.cast(ctypes.c_void_p(vtable_ptr.value), ctypes.POINTER(ctypes.c_void_p))[index]
    prototype = ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, *argtypes)
    function = prototype(function_addr)
    try:
        return int(function(ctypes.c_void_p(pointer), *args))
    except OSError as exc:  # WINFUNCTYPE auto-raises on a failed HRESULT
        return int(exc.winerror) if exc.winerror else -1


def _com_release(pointer: int | None) -> None:
    """Release a COM interface pointer (IUnknown::Release, slot 2); never raises."""
    if not pointer:
        return
    try:
        _com_call(pointer, _COM_RELEASE, ())
    except Exception:  # noqa: BLE001,S110 - a failed release must never break an observation
        pass


class UiaSemanticReader:
    """Warm UIA snapshot reader: focused element + bounded children (raw ctypes COM).

    ``value_source = "uia"`` (WP12e): UIA values are NOT truncated at the
    ``_WINDOW_TEXT_MAX_CHARS`` cap, so the backend's cap-blind rule does not
    apply to them (only the Win32 fallback truncates).

    - ``warm()`` at backend init pays the one-time COM/class-object cost (~200 ms) so
      per-observation reads stay in the ~1-10 ms range (research digest Q3).
    - ``read()`` returns ``{"focused": {...}, "elements": [...]}`` bounded by
      ``UIA_MAX_ELEMENTS`` / ``UIA_MAX_DEPTH`` and a hard wall-clock budget; every
      failure degrades to ``None`` — an observation must never fail because of UIA.
    - All calls go through :func:`_com_call` on fixed UIAutomationClient.h vtable
      slots (IUnknown 0-2; IUIAutomation::GetFocusedElement=7,
      CreateTrueCondition=22; IUIAutomationElement::FindAll=6,
      GetCurrentPropertyValue=10; IUIAutomationElementArray::get_Length=3,
      GetElement=4). Property reads use ``GetCurrentPropertyValue`` exclusively so
      only five vtable slots are load-bearing.
    """

    value_source = "uia"

    def __init__(self) -> None:
        self._automation: int | None = None
        self._available = False
        self._lock = threading.Lock()
        self._com_initialized_threads: set[int] = set()
        # WP13 DELTA-2: ONE daemon owner thread runs EVERY UIA call for this
        # reader's whole life — COM is initialized and the automation object is
        # created THERE, and no other thread ever touches the raw pointer.
        # DELTA-3 m3: on stop, the owner releases the pointer + CoUninitializes.
        self._worker = _SingleOwnerComWorker(on_stop=self._release_com_on_owner)
        #: Typed degradation of the LAST read: ``"read_timeout"`` when the wall
        #: expired (or the owner was still busy with a hung provider), ``None``
        #: otherwise. The backend turns it into the typed unverified branch
        #: ``branch=read_timeout`` — the degradation is VISIBLE, never silent.
        self.last_degraded_reason: str | None = None
        #: DELTA-3 m4: the warm's resolution — ``None`` (still in flight /
        #: not started), ``"ok"``, ``"timeout"`` (still pending) or ``"failed"``.
        #: The backend lazily swaps to the Win32 reader only on ``"failed"``.
        #: N1 (VAL-WP13-02): written by BOTH the owner (final outcome) and the
        #: warm caller (pending timeout) — guard the compare-and-set.
        self.warm_state: str | None = None
        self._warm_state_lock = threading.Lock()

    @property
    def owner_thread_ident(self) -> int:
        """The COM OWNER thread's ident (every UIA call runs on it — test seam)."""
        return self._worker.owner_ident

    def stop_owner_thread(self) -> None:
        """DELTA-3 m3: stop the COM owner thread (releases the CUIAutomation
        pointer ON the owner thread + CoUninitialize). Idempotent, never blocks."""
        self._worker.stop()

    def _release_com_on_owner(self) -> None:
        """Runs ON the owner thread (worker stop sentinel): release the cached
        ``IUIAutomation`` pointer and CoUninitialize the apartment. No other
        thread may touch the pointer (single-owner doctrine)."""
        with self._lock:
            if self._automation:
                try:
                    _com_call(self._automation, _COM_RELEASE, ())
                except Exception:  # noqa: BLE001 - shutdown is best-effort
                    pass
                self._automation = None
            self._available = False
            if self._com_initialized_threads and _ole32 is not None:
                try:
                    _ole32.CoUninitialize()
                except (OSError, AttributeError):
                    pass
                self._com_initialized_threads.clear()

    @property
    def available(self) -> bool:
        return self._available

    def _ensure_com(self) -> bool:
        """CoInitializeEx on the calling thread (idempotent; mode-change tolerated)."""
        if not IS_WINDOWS or _ole32 is None:
            return False
        thread_id = threading.get_ident()
        if thread_id in self._com_initialized_threads:
            return True
        try:
            result = int(_ole32.CoInitializeEx(None, _COINIT_APARTMENTTHREADED))
        except (OSError, AttributeError):
            return False
        if result not in (0, _RPC_E_CHANGED_MODE):
            return False
        self._com_initialized_threads.add(thread_id)
        return True

    def _ensure_automation(self) -> int | None:
        """CoCreateInstance(CLSID_CUIAutomation) once; returns the cached interface pointer."""
        with self._lock:
            if self._automation:
                return self._automation
            if not self._ensure_com() or _ole32 is None:
                return None
            clsid = _guid_from_string(_CLSID_CUIAUTOMATION)
            iid = _guid_from_string(_IID_IUIAUTOMATION)
            out = ctypes.c_void_p()
            try:
                result = int(
                    _ole32.CoCreateInstance(
                        ctypes.byref(clsid), None, _CLSCTX_INPROC_SERVER, ctypes.byref(iid), ctypes.byref(out)
                    )
                )
            except (OSError, AttributeError):
                return None
            if result != 0 or not out.value:
                return None
            self._automation = int(out.value)
            self._available = True
            return self._automation

    def _set_warm_state(self, value: str, only_if_pending: bool = False) -> None:
        """Record the warm's resolution (N1, VAL-WP13-02): the OWNER thread's
        final ``"ok"``/``"failed"`` must NEVER be overwritten by the caller's
        late ``"timeout"`` — the compare-and-set is lock-guarded."""
        with self._warm_state_lock:
            if only_if_pending and self.warm_state is not None:
                return
            self.warm_state = value

    def warm(self) -> str:
        """Warm the COM apartment + automation object on the OWNER thread; one
        throwaway focused read.

        Returns ``"ok"`` (warm completed), ``"timeout"`` (the wall expired —
        the reader STAYS the active UIA reader: the queued warm may still
        complete later, and the caller must NOT swap readers on a timeout) or
        ``"failed"`` (the warm ran and genuinely failed — swap to the Win32
        fallback is correct). Never raises: a failure leaves the reader
        permanently unavailable and the observation's semantic fields stay
        ``None`` (silent degradation). The warm runs under the derived
        WARM wall (:func:`semantic_warm_timeout_seconds`, the full call-budget
        share capped at 10 s) — a cold first CoCreateInstance on a slow machine
        can exceed the per-read wall (WP13 DELTA-2).

        N1 (VAL-WP13-02): the OUTCOME is recorded ON THE OWNER THREAD —
        ``_warm_unbounded`` sets the final ``"ok"``/``"failed"`` itself when it
        finishes, so a warm that times out and LATER fails still resolves to
        ``"failed"`` (the backend's lazy Win32 swap fires on the next read);
        the caller's late ``"timeout"`` only lands while the state is still
        pending. When the owner already resolved by the time the caller gives
        up, the RESOLVED state is returned.
        """
        result, timed_out = self._worker.run_bounded(
            self._warm_unbounded, semantic_warm_timeout_seconds()
        )
        if timed_out:
            self.last_degraded_reason = "read_timeout"
            self._set_warm_state("timeout", only_if_pending=True)
            return self.warm_state
        self.last_degraded_reason = None
        if self.warm_state is None:
            # the owner body records its own outcome; this fallback only
            # covers warm bodies that predate the recording (never shipped)
            self._set_warm_state("ok" if bool(result) else "failed")
        return self.warm_state

    def _warm_unbounded(self) -> bool:
        """The warm body, run ON the owner thread: COM init + activation + the
        throwaway focused read. M2 (VAL-WP13-01): the read calls
        ``_read_unbounded`` DIRECTLY — ``self.read()`` would re-enter
        ``run_bounded`` from the owner thread and be swallowed by the busy
        gate, silently skipping the cold focused-read cost the warm exists to
        pay. N1 (VAL-WP13-02): the body records its OWN final outcome
        (``"ok"``/``"failed"``) here, on the owner thread."""
        try:
            automation = self._ensure_automation()
            if not automation:
                self._set_warm_state("failed")
                return False
            self._read_unbounded()  # one throwaway read; warms focused-element + property paths
            self._set_warm_state("ok" if self._available else "failed")
            return self._available
        except Exception:  # noqa: BLE001 - warm must never break backend construction
            self._set_warm_state("failed")
            return False

    # -- property reads ------------------------------------------------------------------

    def _read_property(self, element: int, property_id: int) -> tuple[int, object]:
        """Raw ``GetCurrentPropertyValue`` result as ``(variant_type, python_value)``."""
        variant = _VARIANT()
        result = _com_call(
            element, _UIA_ELEMENT_GET_PROPERTY_VALUE, (ctypes.c_long, ctypes.POINTER(_VARIANT)),
            property_id, ctypes.byref(variant),
        )
        if result != 0 or variant.vt == _VT_EMPTY:
            return (_VT_EMPTY, None)
        variant_type = int(variant.vt)  # captured before VariantClear resets it
        if variant_type == _VT_BSTR:
            value = ctypes.c_wchar_p(variant.union.bstrVal).value if variant.union.bstrVal else None
        elif variant_type == _VT_I4:
            value = int(variant.union.lVal)
        elif variant_type == _VT_BOOL:
            value = bool(variant.union.boolVal)
        elif variant_type == (_VT_ARRAY | _VT_R8):
            value = _safe_array_doubles(variant.union.parray)
        else:
            value = None
        try:
            _oleaut32.VariantClear(ctypes.byref(variant))
        except (OSError, AttributeError, ValueError):
            pass
        return (variant_type, value)

    def _element_summary(
        self, element: int, deadline: float, *, focused: bool = False
    ) -> dict[str, object]:
        """Bounded summary dict for one element (property reads + rect)."""
        _vtype, name = self._read_property(element, _UIA_PROP_NAME)
        if time.perf_counter() > deadline:
            name = None  # budget exceeded: still return the partial summary
        _vtype, control_type = self._read_property(element, _UIA_PROP_CONTROL_TYPE)
        _vtype, automation_id = self._read_property(element, _UIA_PROP_AUTOMATION_ID)
        _vtype, value = self._read_property(element, _UIA_PROP_VALUE)
        _vtype, offscreen = self._read_property(element, _UIA_PROP_IS_OFFSCREEN)
        _vtype, rect = self._read_property(element, _UIA_PROP_BOUNDING_RECTANGLE)
        rect_tuple = (
            tuple(float(item) for item in rect[:4]) if isinstance(rect, tuple) and len(rect) >= 4 else None
        )
        return {
            "name": name if isinstance(name, str) else None,
            "control_type": UIA_CONTROLTYPE_NAMES.get(int(control_type)) if isinstance(control_type, int) else None,
            "automation_id": automation_id if isinstance(automation_id, str) else None,
            "value": value if isinstance(value, str) else None,
            "offscreen": bool(offscreen) if isinstance(offscreen, bool) else None,
            "rect": rect_tuple,
            "focused": focused,
        }

    @staticmethod
    def _dropped_for_total(total: int, *, visited: int | None = None) -> dict[str, int]:
        """Breakdown of the elements a bounded walk of ``total`` did not hand over.

        ``total`` is what the container PROMISED (``IUIAutomationElementArray::get_Length``).
        ``visited`` is how many the walk actually reached; ``None`` means the walk
        stopped at the element cap, so the remainder is a ``cap_exceeded`` loss, and an
        explicit value splits the remainder into a budget loss (what the cap did not
        explain) and nothing else.
        """
        total = max(0, int(total))
        if visited is None:
            return {**_empty_drops(), "cap_exceeded": max(0, total - UIA_MAX_ELEMENTS)}
        reached = max(0, min(int(visited), total))
        return {**_empty_drops(), "budget_exceeded": max(0, total - reached)}

    @classmethod
    def _account_cap_and_budget(
        cls, drops: dict[str, int], total: int, *, visited: int
    ) -> None:
        """ACCUMULATE the cap/budget split into the walk's own drop counts (E-15).

        E-15 (ORVEX-2026-106, fixed during the WP4b port): the dev copy merged this
        split with ``drops.update(...)``, which REPLACES the values for
        ``cap_exceeded``/``budget_exceeded`` — and because the split dict carries all
        four reasons (via ``_empty_drops``), the update also ZEROED the
        ``offscreen``/``unavailable`` counts the loop had already accumulated. A read
        that dropped 3 offscreen elements and hit the cap reported
        ``offscreen: 0``. Accumulation is the fix: add the split's counts onto the
        loop's own, never overwrite them.
        """
        if visited >= UIA_MAX_ELEMENTS:
            extra = cls._dropped_for_total(total)
        elif visited < total:
            extra = cls._dropped_for_total(total, visited=visited)
        else:
            return
        for reason, count in extra.items():
            drops[reason] += count

    def read(self) -> dict[str, object] | None:
        """One bounded semantic snapshot; ``None`` on any failure (silent degradation).

        D15 (ORVEX-2026-108 WP4b): the snapshot now carries a ``dropped`` breakdown
        under :data:`SNAPSHOT_DROPPED_KEY` for every element the walk SAW but did not
        hand over — the ``UIA_MAX_ELEMENTS`` cap, the wall-clock budget break, offscreen
        elements, and elements whose summary could not be read. Nothing is silently
        dropped any more: the breakdown rides the existing additive ``omitted_count``
        channel of the ``spatial_text`` block.

        WP13 DELTA-2: the whole read runs on the reader's SINGLE COM OWNER
        thread under the derived semantic-read wall (:meth:`_SingleOwnerComWorker.
        run_bounded`) — the internal 0.05 s budget is only checked BETWEEN COM
        calls, and one hung WebView2/Chromium provider call (field evidence:
        WhatsApp) can otherwise block the calling tool forever. A wall expiry
        (or a read while a previous hung call is still in flight) sets
        :attr:`last_degraded_reason` to ``"read_timeout"`` — the visible,
        typed degradation the typing ladder reports as ``branch=read_timeout``.
        """
        result, timed_out = self._worker.run_bounded(
            self._read_unbounded, semantic_read_timeout_seconds()
        )
        if timed_out:
            self.last_degraded_reason = "read_timeout"
            return None
        self.last_degraded_reason = None
        return result if isinstance(result, dict) or result is None else None

    def _read_unbounded(self) -> dict[str, object] | None:
        if not IS_WINDOWS or _ole32 is None or _oleaut32 is None:
            return None
        deadline = time.perf_counter() + UIA_READ_BUDGET_SECONDS
        try:
            automation = self._ensure_automation()
            if not automation:
                return None
            focused_ptr = ctypes.c_void_p()
            if _com_call(automation, _UIA_GET_FOCUSED_ELEMENT, (ctypes.POINTER(ctypes.c_void_p),), ctypes.byref(focused_ptr)) != 0:
                return None
            if not focused_ptr.value:
                return None
            drops = _empty_drops()
            snapshot: dict[str, object] = {
                "focused": self._element_summary(int(focused_ptr.value), deadline, focused=True),
                "elements": [],
            }
            condition_ptr = ctypes.c_void_p()
            if _com_call(automation, _UIA_CREATE_TRUE_CONDITION, (ctypes.POINTER(ctypes.c_void_p),), ctypes.byref(condition_ptr)) == 0 and condition_ptr.value:
                array_ptr = ctypes.c_void_p()
                found = _com_call(
                    int(focused_ptr.value),
                    _UIA_ELEMENT_FIND_ALL,
                    (ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)),
                    _TREE_SCOPE_CHILDREN,
                    ctypes.c_void_p(condition_ptr.value),
                    ctypes.byref(array_ptr),
                )
                if found == 0 and array_ptr.value:
                    elements: list[dict[str, object]] = []
                    length = ctypes.c_long(0)
                    if _com_call(int(array_ptr.value), _UIA_ARRAY_GET_LENGTH, (ctypes.POINTER(ctypes.c_long),), ctypes.byref(length)) == 0:
                        total = int(length.value)
                        visited = 0
                        for index in range(min(total, UIA_MAX_ELEMENTS)):
                            if time.perf_counter() > deadline:
                                break
                            visited += 1
                            child_ptr = ctypes.c_void_p()
                            if _com_call(int(array_ptr.value), _UIA_ARRAY_GET_ELEMENT, (ctypes.c_long, ctypes.POINTER(ctypes.c_void_p)), index, ctypes.byref(child_ptr)) == 0 and child_ptr.value:
                                summary = self._element_summary(int(child_ptr.value), deadline)
                                if summary.get("offscreen") is not True:
                                    elements.append(summary)
                                else:
                                    drops["offscreen"] += 1
                                _com_release(int(child_ptr.value))
                            else:
                                # the container PROMISED this element and the walk could
                                # not read it — a known loss, reported not swallowed
                                drops["unavailable"] += 1
                        # E-15: ACCUMULATE the cap/budget split — see
                        # :meth:`_account_cap_and_budget` for the overwrite this fixes.
                        self._account_cap_and_budget(drops, total, visited=visited)
                    snapshot["elements"] = elements
                    snapshot[SNAPSHOT_DROPPED_KEY] = drops
                    _com_release(int(array_ptr.value))
                _com_release(int(condition_ptr.value))
            _com_release(int(focused_ptr.value))
            return snapshot
        except Exception:  # noqa: BLE001 - observation must never fail because of UIA
            return None


#: Window-class -> UIA-like control-type name (Win32 fallback; best-effort mapping).
_WIN32_CLASS_CONTROL_TYPES: dict[str, str] = {
    "EDIT": "Edit",
    "RICHEDIT": "Edit",
    "RICHEDIT50W": "Edit",
    "BUTTON": "Button",
    "STATIC": "Text",
    "COMBOBOX": "ComboBox",
    "LISTBOX": "List",
    "SYSLISTVIEW32": "List",
    "SYSHEADER32": "Header",
    "TOOLBARWINDOW32": "ToolBar",
    "STATUSCLASSNAME": "StatusBar",
    "MSCOMCTL.SLIDER": "Slider",
    "MSCTLS_TRACKBAR32": "Slider",
    "MSCTLS_UPDOWN32": "Spinner",
    "MSCTLS_PROGRESS32": "ProgressBar",
    "PROGRESSCLASS": "ProgressBar",
    "TABCONTROL": "Tab",
    "SYSTABCONTROL32": "Tab",
    "SYSDATAGRID": "DataGrid",
    "SCROLLBAR": "ScrollBar",
    "TREEVIEW": "Tree",
    "SYSTREEVIEW32": "Tree",
    "DATETIMEPICK": "Calendar",
    "SYSMONTHCAL32": "Calendar",
}


class Win32TextReader:
    """Win32-window-text semantic reader: the sanctioned fallback when raw UIA COM is
    unavailable (see the module-level rationale on :class:`UiaSemanticReader`).

    ``value_source = "win32"`` (WP12e): this reader TRUNCATES control text at
    ``_WINDOW_TEXT_MAX_CHARS`` — values at the cap are treated as blind
    (branch=read_truncated) by the backend's integrity ladder.

    Emits the SAME snapshot shape as the UIA reader (``focused`` + bounded ``elements``
    list, every element carrying ``name``/``control_type``/``automation_id``/``value``/
    ``offscreen``/``rect``/``focused``/``source``), sourced from:

    - ``GetGUIThreadInfo`` — the foreground thread's focused control (``hwndFocus``);
    - ``WM_GETTEXT``/``WM_GETTEXTLENGTH`` via ``SendMessageTimeoutW`` (aborts on a hung
      target) — control text becomes ``name`` (buttons/menus) and ``value`` (edits);
    - a BOUNDED child walk (``GetWindow(GW_CHILD)``/``GW_HWNDNEXT``): direct children of
      the foreground root, then the children of the first few of those — at most
      ``UIA_MAX_DEPTH`` levels, ``UIA_MAX_ELEMENTS`` elements, and one wall-clock budget,
      same as the UIA reader.

    Honest limitations (documented in the change-log): ``automation_id`` is always None
    (no Win32 equivalent), ``control_type`` is derived from the window class name, and
    names are limited to text the app exposes as window text.
    """

    value_source = "win32"

    def __init__(self) -> None:
        self._available = IS_WINDOWS and _user32 is not None
        #: Parity with UiaSemanticReader: this reader's reads are bounded by
        #: SendMessageTimeoutW (abort-if-hung), never by the COM owner wall, so
        #: no degradation reason is ever set here (backend getattr default).
        self.last_degraded_reason: str | None = None

    @property
    def available(self) -> bool:
        return self._available

    def warm(self) -> bool:
        """No COM apartment to warm; availability is the Win32 binding check."""
        return self._available

    def read(self) -> dict[str, object] | None:
        """One bounded snapshot of the foreground window's focused control + children.

        D15 (ORVEX-2026-108 WP4b): as with the UIA reader, the snapshot carries a
        ``dropped`` breakdown for every child the bounded walk saw but did not
        summarize (built directly by :meth:`_bounded_children` — the dev copy's
        separate ``_dropped_for_total`` helper was DEAD code there, never called by
        this walk, and is deliberately not ported).
        """
        if not IS_WINDOWS or _user32 is None:
            return None
        deadline = time.perf_counter() + UIA_READ_BUDGET_SECONDS
        try:
            root = int(_user32.GetForegroundWindow() or 0)
            if not root:
                return None
            focused_summary = self._focused_control_summary(root)
            elements, drops = self._bounded_children(root, deadline)
            return {
                "focused": focused_summary,
                "elements": elements,
                "source": "win32",
                SNAPSHOT_DROPPED_KEY: drops,
            }
        except Exception:  # noqa: BLE001 - observation must never fail because of UIA
            return None

    def _focused_control_summary(self, root: int) -> dict[str, object] | None:
        """The foreground thread's focused control via ``GetGUIThreadInfo``."""
        try:
            thread_id = _user32.GetWindowThreadProcessId(ctypes.c_void_p(root), None)
            info = _GUITHREADINFO()
            info.cbSize = ctypes.sizeof(_GUITHREADINFO)
            if not thread_id or not _user32.GetGUIThreadInfo(thread_id, ctypes.byref(info)):
                return None
            hwnd_focus = int(info.hwndFocus or 0)
            if not hwnd_focus:
                return None
            return self._control_summary(hwnd_focus, focused=True)
        except (OSError, AttributeError, ValueError):
            return None

    def _bounded_children(
        self, root: int, deadline: float
    ) -> tuple[list[dict[str, object]], dict[str, int]]:
        """Two-level, count- and budget-bounded walk of the foreground window's children.

        Returns the elements it summarized AND the :data:`SNAPSHOT_DROP_REASONS`
        breakdown of the children the walk reached but could not hand over (D15).
        """
        elements: list[dict[str, object]] = []
        drops = _empty_drops()
        try:
            level1 = _window_child_chain(root, limit=UIA_MAX_ELEMENTS)
            for index, child in enumerate(level1):
                if len(elements) >= UIA_MAX_ELEMENTS or time.perf_counter() > deadline:
                    # DELTA 1 (VAL-WP4b MINOR): EVERY sibling the chain still holds is
                    # a child the walk reached but did not hand over — not just one.
                    drops["budget_exceeded"] += len(level1) - index
                    break
                summary = self._control_summary(child)
                if summary is not None:
                    elements.append(summary)
                else:
                    drops["unavailable"] += 1
                if index < 3:  # budget guard: only the first few hosts get a second level
                    level2 = _window_child_chain(child, limit=UIA_MAX_ELEMENTS - len(elements))
                    for grandchild_index, grandchild in enumerate(level2):
                        if len(elements) >= UIA_MAX_ELEMENTS or time.perf_counter() > deadline:
                            drops["budget_exceeded"] += len(level2) - grandchild_index
                            break
                        summary = self._control_summary(grandchild)
                        if summary is not None:
                            elements.append(summary)
                        else:
                            drops["unavailable"] += 1
                    if drops["budget_exceeded"] or len(elements) >= UIA_MAX_ELEMENTS:
                        # The early exit after a filled second level must ALSO count the
                        # level-1 siblings it is about to abandon (the same
                        # "healthy-looking partial read" failure D15 targets: a break
                        # that counts +1 while several siblings remain).
                        drops["budget_exceeded"] += len(level1) - (index + 1)
                        break
        except (OSError, AttributeError, ValueError):
            return elements, drops
        return elements, drops

    def _control_summary(self, hwnd: int, *, focused: bool = False) -> dict[str, object] | None:
        """One control's summary dict; None when the window is gone or inaccessible."""
        try:
            if not _user32.IsWindow(ctypes.c_void_p(hwnd)):
                return None
            class_name = _window_class_name(hwnd)
            text = _window_text_via_message(hwnd)
            visible = bool(_user32.IsWindowVisible(ctypes.c_void_p(hwnd)))
            rect = _window_rect(hwnd)
            control_type = _WIN32_CLASS_CONTROL_TYPES.get((class_name or "").upper(), class_name)
            value = text if (focused or (class_name or "").upper() in {"EDIT", "RICHEDIT", "RICHEDIT50W"}) else None
            return {
                "name": text or None,
                "control_type": control_type,
                "automation_id": None,
                "value": value or None,
                "offscreen": not visible,
                "rect": tuple(float(item) for item in rect) if rect else None,
                "focused": focused,
                "source": "win32",
            }
        except (OSError, AttributeError, ValueError):
            return None


def _window_child_chain(parent: int, *, limit: int) -> list[int]:
    """Sibling chain of ``parent``'s direct children via GetWindow (bounded, read-only)."""
    chain: list[int] = []
    if not IS_WINDOWS or _user32 is None or limit <= 0:
        return chain
    try:
        handle = int(_user32.GetWindow(ctypes.c_void_p(parent), _GW_CHILD) or 0)
        while handle and len(chain) < limit:
            chain.append(handle)
            handle = int(_user32.GetWindow(ctypes.c_void_p(handle), _GW_HWNDNEXT) or 0)
    except (OSError, AttributeError, ValueError):
        return chain
    return chain


def _window_text_via_message(hwnd: int, max_chars: int = _WINDOW_TEXT_MAX_CHARS) -> str:
    """Control text via WM_GETTEXT (SendMessageTimeoutW, abort-if-hung); '' on failure.

    FIX2 (2026-09-13, evidence `evidence/v06-006/eng-input/fix2/fix2_readtest.json`):
    the previous two-call sequence (WM_GETTEXTLENGTH to size the buffer, then
    WM_GETTEXT) returned ONE character for a 20-character edit on the reference
    desktop — the cross-thread ``WM_GETTEXTLENGTH`` answer via ``SendMessageTimeoutW``
    mis-reports the length there (the same query via blocking ``SendMessageW``
    returns 20, and a fixed-buffer ``WM_GETTEXT`` returns the full text). That
    mis-measurement made the R-04 integrity read-back see false-short buffers,
    triggering wrongful retypes whose text then REALLY landed — the double-apply
    duplication the integrated-RC live proofs caught (buffers like
    ``#00 alpha 0. end00 alpha 0. end``). The read now uses ONE WM_GETTEXT call with
    a fixed generous buffer (WM_GETTEXT truncates safely at the buffer size and
    reports the chars copied), and the cap default is large enough that edit-control
    reads return the WHOLE text — the tail comparison in the integrity verifier
    depends on it (WM_GETTEXT returns text from the START of the buffer).

    Window-class side effect: none (read-only query, abort-if-hung on a hung target).
    """
    if not IS_WINDOWS or _user32 is None or not hwnd:
        return ""
    try:
        size = max_chars + 1
        buffer = ctypes.create_unicode_buffer(size)
        result = ctypes.c_size_t(0)
        copied = int(_user32.SendMessageTimeoutW(
            ctypes.c_void_p(hwnd), _WM_GETTEXT, size, buffer, _SMTO_ABORTIFHUNG, _SENDMESSAGE_TIMEOUT_MS, ctypes.byref(result)
        ) or 0)
        if not copied:
            return ""
        return buffer.value[:max_chars]
    except (OSError, AttributeError, ValueError):
        return ""


def _safe_array_doubles(pointer: int | None) -> tuple[float, ...] | None:
    """Read a 1-D SAFEARRAY of doubles (UIA BoundingRectangle); None on any failure."""
    if not pointer or _oleaut32 is None:
        return None
    access = ctypes.c_void_p()
    try:
        if int(_oleaut32.SafeArrayAccessData(ctypes.c_void_p(pointer), ctypes.byref(access))) != 0:
            return None
        if not access.value:
            return None
        values = tuple(float(ctypes.cast(ctypes.c_void_p(access.value), ctypes.POINTER(ctypes.c_double))[index]) for index in range(4))
        return values
    except (OSError, AttributeError, ValueError, IndexError):
        return None
    finally:
        try:
            _oleaut32.SafeArrayUnaccessData(ctypes.c_void_p(pointer))
        except (OSError, AttributeError, ValueError):
            pass


def _uia_rect_to_screenshot(
    rect: tuple[float, float, float, float] | None,
    origin: tuple[int, int],
    scale: tuple[float, float],
) -> tuple[int, int, int, int] | None:
    """Convert a physical UIA rect to screenshot-local ``(x, y, w, h)`` (None if empty)."""
    if rect is None:
        return None
    left, top, width, height = rect
    if width <= 0 or height <= 0:
        return None
    scale_x = scale[0] if scale[0] > 0 else 1.0
    scale_y = scale[1] if scale[1] > 0 else 1.0
    x = max(0, round((left - origin[0]) / scale_x))
    y = max(0, round((top - origin[1]) / scale_y))
    return (x, y, max(1, round(width / scale_x)), max(1, round(height / scale_y)))


def _uia_snapshot_to_ui_elements(snapshot: dict[str, object]) -> list[dict[str, object]]:
    """Snapshot -> ``Observation.ui_elements`` payload (focused element first)."""
    elements: list[dict[str, object]] = []
    focused = snapshot.get("focused")
    if isinstance(focused, dict):
        elements.append(focused)
    children = snapshot.get("elements")
    if isinstance(children, list):
        for element in children:
            if isinstance(element, dict):
                elements.append(element)
    return elements[: UIA_MAX_ELEMENTS + 1]


#: Per-region label clip applied by the semantic read (unchanged bound; D15 only MARKS
#: the clip, it does not lift it).
UIA_MAX_LABEL_CHARS = 200


def _uia_snapshot_to_text_regions(
    snapshot: dict[str, object],
    origin: tuple[int, int],
    scale: tuple[float, float],
) -> tuple[list[TextRegion], int]:
    """Snapshot -> ``(Observation.ocr_text regions, known-but-dropped count)``.

    D15 (ORVEX-2026-108 WP4b). Two things changed, both additive:

    1. The function now RETURNS the number of elements it saw but could not turn into a
       region — the ``UIA_MAX_ELEMENTS`` cap here, rect-less/degenerate elements, and
       text-less elements — PLUS whatever the reader itself reported under
       :data:`SNAPSHOT_DROPPED_KEY`. Those are added, never double-counted: a reader
       that dropped an element did not put it in the list this function iterates, so
       the two channels are disjoint by construction.
    2. A label longer than :data:`UIA_MAX_LABEL_CHARS` is served clipped **and** marked
       ``text_truncated`` on the region, so a silent content loss cannot pass as a
       complete read.
    """
    regions: list[TextRegion] = []
    dropped = snapshot_dropped_total(snapshot.get(SNAPSHOT_DROPPED_KEY))
    candidates: list[object] = []
    focused = snapshot.get("focused")
    if isinstance(focused, dict):
        candidates.append(focused)
    children = snapshot.get("elements")
    if isinstance(children, list):
        candidates.extend(item for item in children if isinstance(item, dict))
    for element in candidates:
        if len(regions) >= UIA_MAX_ELEMENTS:
            dropped += 1
            continue
        rect = element.get("rect")
        if not isinstance(rect, tuple) or len(rect) != 4:
            dropped += 1
            continue
        converted = _uia_rect_to_screenshot(
            (float(rect[0]), float(rect[1]), float(rect[2]), float(rect[3])), origin, scale
        )
        if converted is None:
            dropped += 1
            continue
        text = element.get("name") or element.get("control_type") or ""
        if not text:
            dropped += 1
            continue
        x, y, width, height = converted
        regions.append(
            TextRegion(
                text=text[:UIA_MAX_LABEL_CHARS],
                x=x,
                y=y,
                width=width,
                height=height,
                text_truncated=len(text) > UIA_MAX_LABEL_CHARS,
            )
        )
    return regions, dropped


# Bottom-of-module import: windows.query_focus_target reads this reader and
# Win32TextReader above reads windows' class/rect queries — call-time-only
# mutual use, resolved by importing each side after both modules are defined.
from .windows import _window_class_name, _window_rect  # noqa: E402
