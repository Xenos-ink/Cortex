"""Screen capture: the mss BitBlt pipeline and the DXGI Desktop Duplication fast path."""

from __future__ import annotations

import base64
import hashlib
import struct

from dataclasses import dataclass

from typing import TYPE_CHECKING

from PIL import Image

from .coordinates import CoordinateTransform

if TYPE_CHECKING:  # annotation-only names (lazy under __future__ annotations)
    from ..core.models import MonitorInfo
    from .coordinates import CoordinateVerdict

PNG_OPTIMIZE_ENV = "CORTEX_PNG_OPTIMIZE"  # "1" restores optimize=True (default off)
PNG_COMPRESS_LEVEL_ENV = "CORTEX_PNG_COMPRESS_LEVEL"  # 0..9 PIL compress_level (default: PIL 6 = today's bytes)
INTERNAL_FRAME_REUSE_ENV = "CORTEX_INTERNAL_FRAME_REUSE"  # "0" disables R-5 frame/payload reuse (default on)
# R-6 capture backend: "blt" (default) keeps the R-5 GDI/mss BitBlt pipeline;
# "dxgi" switches the per-monitor pixel grab to Desktop Duplication (measured
# 25.5ms vs 53ms per changed-frame capture on the mission desktop, pure ctypes,
# no new dependency). Any DXGI failure at ANY point falls back to the mss grab
# for that capture — the fast path can only speed things up, never reject.
CAPTURE_BACKEND_ENV = "CORTEX_CAPTURE"  # "blt" (default) | "dxgi"
_DXGI_ACQUIRE_TIMEOUT_MS = 8  # AcquireNextFrame wait when the desktop is idle
# R-6 encode-skip: session-level knob. The SERVER sets this attribute True on the
# session backend when image_delivery="text" (text-mode sessions never emit an
# image block, so a lossless PNG payload exists only to be hashed for the digest
# and compared for staleness — a deterministic raw-pixel key serves both cheaper).
# CORTEX_TEXT_PNG=1 is the kill-switch: text sessions keep encoding real PNGs.
TEXT_PNG_ENV = "CORTEX_TEXT_PNG"  # "1" forces PNG payloads even in text sessions


@dataclass(frozen=True)
class _CaptureContext:
    """Coordinate context recorded by the most recent observe() on this backend."""

    monitor: MonitorInfo
    verdict: CoordinateVerdict

    @property
    def transform(self) -> CoordinateTransform:
        return CoordinateTransform(
            origin_x=self.monitor.bounds[0],
            origin_y=self.monitor.bounds[1],
            scale_x=self.verdict.scale_x,
            scale_y=self.verdict.scale_y,
        )


class _DxgiDuplicator:
    """R-6: pure-ctypes DXGI Desktop Duplication grabber (no new dependency).

    The COM plumbing (factory -> adapter -> output -> IDXGIOutput1::DuplicateOutput
    -> D3D11 staging-texture readback) with vtable slots verified against the
    Windows SDK 10.0.18362.0 C-interface Vtbl structs. Semantics that matter to
    the caller:

    - Desktop Duplication yields a frame ONLY when the compositor presented a
      desktop change; on an idle desktop ``grab`` times out and returns the
      previously read frame (which is still the current desktop image).
    - ``grab`` NEVER raises for a timeout; any structural failure raises
      ``OSError`` and the CALLER (LocalComputerBackend) falls back to the mss
      BitBlt grab — the capture path is fail-open, never fail-closed.
    - The output is a PIL RGB image, pixel-equivalent to the mss BGRX decode of
      the same instant (verified: mean-diff 0.003 between back-to-back DXGI and
      GDI grabs on a quiet screen; residual = in-flight animation).

    The class is import-light (ctypes + struct; PIL imported lazily) and is only
    constructed when ``CORTEX_CAPTURE=dxgi`` asked for it.
    """

    __slots__ = (
        "width", "height", "rotation",
        "_factory", "_adapter", "_output", "_output1", "_device", "_context",
        "_dup", "_dupv", "_stage", "_last_img",
    )

    _DXGI_ERROR_WAIT_TIMEOUT = 0x087A0001
    #: R-6a: the first frame of a fresh duplication composites in ~58 ms on this
    #: hardware; the one-shot cold-start retry uses this budget (never steady-state).
    _FIRST_FRAME_TIMEOUT_MS = 60

    # IIDs from the SDK DEFINE_GUID lines (Data1 u32 LE, Data2/Data3 u16 LE).
    _IID_IDXGIFACTORY1 = (
        0x770AAE78.to_bytes(4, "little") + 0xF26F.to_bytes(2, "little")
        + 0x4DBA.to_bytes(2, "little")
        + bytes([0xA8, 0x29, 0x25, 0x3C, 0x83, 0xD1, 0xB3, 0x87])
    )
    _IID_IDXGIOUTPUT1 = (
        0x00CDDEA8.to_bytes(4, "little") + 0x939B.to_bytes(2, "little")
        + 0x4B83.to_bytes(2, "little")
        + bytes([0xA3, 0x40, 0xA6, 0x85, 0x22, 0x66, 0x66, 0xCC])
    )
    _IID_ID3D11TEXTURE2D = (
        0x6F15AAF2.to_bytes(4, "little") + 0xD208.to_bytes(2, "little")
        + 0x4E89.to_bytes(2, "little")
        + bytes([0x9A, 0xB4, 0x48, 0x95, 0x35, 0xD3, 0x4F, 0x9C])
    )

    # Verified C-vtable slots (see class docstring for the provenance).
    _SLOT_ENUM_ADAPTERS = 7      # IDXGIFactory1::EnumAdapters
    _SLOT_ENUM_OUTPUTS = 7       # IDXGIAdapter::EnumOutputs
    _SLOT_DUP_OUTPUT = 22        # IDXGIOutput1::DuplicateOutput
    _DUP_GET_DESC = 7            # IDXGIOutputDuplication::GetDesc
    _DUP_ACQUIRE = 8             # IDXGIOutputDuplication::AcquireNextFrame
    _DUP_RELEASE_FRAME = 14      # IDXGIOutputDuplication::ReleaseFrame
    _DEV_CREATE_TEX2D = 5        # ID3D11Device::CreateTexture2D
    _CTX_MAP = 14                # ID3D11DeviceContext::Map
    _CTX_UNMAP = 15              # ID3D11DeviceContext::Unmap
    _CTX_COPY_RESOURCE = 47       # ID3D11DeviceContext::CopyResource
    _SLOT_QI = 0
    _SLOT_RELEASE = 2

    def __init__(self, output_index: int = 0) -> None:
        import ctypes  # local import: only the dxgi path pays for it

        self._last_img = None
        dxgi = ctypes.windll.dxgi
        d3d11 = ctypes.windll.d3d11

        def iid(raw: bytes) -> "ctypes.Array":
            return (ctypes.c_byte * 16).from_buffer_copy(raw)

        def vtbl(iface: "ctypes.c_void_p"):
            ptr = ctypes.cast(
                ctypes.cast(iface, ctypes.POINTER(ctypes.c_void_p))[0],
                ctypes.POINTER(ctypes.c_void_p),
            )

            def call(slot: int, restype, argtypes, *args):
                proto = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)
                return proto(ptr[slot])(iface, *args)

            return call

        factory = ctypes.c_void_p()
        hr = dxgi.CreateDXGIFactory1(
            ctypes.byref(iid(self._IID_IDXGIFACTORY1)), ctypes.byref(factory)
        )
        if hr:
            raise OSError(f"CreateDXGIFactory1 hr=0x{hr & 0xFFFFFFFF:08X}")
        self._factory = factory

        adapter = ctypes.c_void_p()
        hr = vtbl(factory)(
            self._SLOT_ENUM_ADAPTERS, ctypes.HRESULT,
            (ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p)),
            0, ctypes.byref(adapter),
        )
        if hr:
            raise OSError(f"EnumAdapters hr=0x{hr & 0xFFFFFFFF:08X}")
        self._adapter = adapter

        output = ctypes.c_void_p()
        hr = vtbl(adapter)(
            self._SLOT_ENUM_OUTPUTS, ctypes.HRESULT,
            (ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p)),
            output_index, ctypes.byref(output),
        )
        if hr:
            raise OSError(f"EnumOutputs hr=0x{hr & 0xFFFFFFFF:08X}")
        self._output = output

        output1 = ctypes.c_void_p()
        # R-6a FIX (dangling-IID): the IID buffer must OUTLIVE the native call.
        # ``ctypes.byref`` returns a CArgObject and ``ctypes.cast`` copies the raw
        # address into a c_void_p that holds NO reference to either — with the array
        # built inline in the call expression, the array (and its byref) can be
        # garbage-collected BEFORE the QI executes, leaving the IID pointer dangling:
        # QI then honestly returns E_NOINTERFACE (heap-state dependent; reproduced
        # live: first-attempt failure, immediate retry success, 8/8 fresh-process
        # failures in one shell and 0/10 with the local bound). Binding the array to
        # a local keeps it alive for the whole call — the same invariant every other
        # argument in this class already satisfies via named locals.
        iid_output1 = iid(self._IID_IDXGIOUTPUT1)
        hr = vtbl(output)(
            self._SLOT_QI, ctypes.HRESULT,
            (ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)),
            ctypes.cast(ctypes.byref(iid_output1), ctypes.c_void_p),
            ctypes.byref(output1),
        )
        if hr:
            raise OSError(f"QI IDXGIOutput1 hr=0x{hr & 0xFFFFFFFF:08X}")
        self._output1 = output1

        create_device = ctypes.WINFUNCTYPE(
            ctypes.HRESULT,
            ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint,
            ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint,
            ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_uint),
            ctypes.POINTER(ctypes.c_void_p),
        )
        device = ctypes.c_void_p()
        feature_level = ctypes.c_uint()
        context = ctypes.c_void_p()
        # NULL adapter + D3D_DRIVER_TYPE_HARDWARE(1) + D3D11_SDK_VERSION(7)
        hr = create_device(d3d11.D3D11CreateDevice)(
            None, 1, None, 0, None, 0, 7,
            ctypes.byref(device), ctypes.byref(feature_level), ctypes.byref(context),
        )
        if hr:
            raise OSError(f"D3D11CreateDevice hr=0x{hr & 0xFFFFFFFF:08X}")
        self._device = device
        self._context = context

        dup = ctypes.c_void_p()
        hr = vtbl(output1)(
            self._SLOT_DUP_OUTPUT, ctypes.HRESULT,
            (ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)),
            device, ctypes.byref(dup),
        )
        if hr:
            raise OSError(f"DuplicateOutput hr=0x{hr & 0xFFFFFFFF:08X}")
        self._dup = dup
        self._dupv = vtbl(dup)

        # DXGI_OUTDUPL_DESC {DXGI_MODE_DESC ModeDesc(24B); Rotation(4); BOOL(4)}
        desc = ctypes.create_string_buffer(40)
        self._dupv(self._DUP_GET_DESC, None, (ctypes.c_void_p,),
                   ctypes.cast(desc, ctypes.c_void_p))
        self.width = int.from_bytes(desc.raw[0:4], "little")
        self.height = int.from_bytes(desc.raw[4:8], "little")
        self.rotation = int.from_bytes(desc.raw[28:32], "little")
        if self.width <= 0 or self.height <= 0:
            raise OSError(f"DXGI_OUTDUPL_DESC has invalid bounds {self.width}x{self.height}")

        # One persistent staging (CPU-readable) texture, reused across grabs.
        # D3D11_TEXTURE2D_DESC: Width, Height, MipLevels, ArraySize, Format,
        # SampleCount, SampleQuality, Usage(STAGING=3), BindFlags, CPUAccess(READ),
        # MiscFlags — 11 UINTs.
        stage_desc = struct.pack(
            "<11I", self.width, self.height, 1, 1, 87, 1, 0, 3, 0, 0x20000, 0
        )
        stage = ctypes.c_void_p()
        # R-6a FIX (dangling-buffer): same lifetime rule as the QI IID — the packed
        # D3D11_TEXTURE2D_DESC buffer must outlive the CreateTexture2D call.
        stage_desc_buffer = ctypes.create_string_buffer(stage_desc)
        hr = vtbl(device)(
            self._DEV_CREATE_TEX2D, ctypes.HRESULT,
            (ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)),
            ctypes.cast(stage_desc_buffer, ctypes.c_void_p),
            None, ctypes.byref(stage),
        )
        if hr:
            raise OSError(f"CreateTexture2D(staging) hr=0x{hr & 0xFFFFFFFF:08X}")
        self._stage = stage

    def grab(self, timeout_ms: int = 8):
        """One frame as a PIL RGB Image (or the last frame on an idle timeout).

        Raises OSError on any structural failure (device lost, access revoked,
        session lock); the backend then falls back to the mss grab. Never
        returns None after the first successful read.

        R-6a first-frame guarantee: a freshly created duplication ALWAYS holds the
        current desktop image, but the first ``AcquireNextFrame`` needs ~58 ms of
        DWM composition on this hardware — a short timeout would starve every
        capture on a quiet desktop until some app animates. When the acquire times
        out and no frame has ever been read, ONE retry runs with the longer
        ``_FIRST_FRAME_TIMEOUT_MS`` budget (bounded to the cold start; steady-state
        keeps the fast 8 ms timeout and returns the cached frame on idle).
        """
        img = self._acquire(timeout_ms)
        if img is None and self._last_img is None:
            img = self._acquire(self._FIRST_FRAME_TIMEOUT_MS)
        return img

    def _acquire(self, timeout_ms: int):
        import ctypes

        frame_info = ctypes.create_string_buffer(64)
        res = ctypes.c_void_p()
        try:
            hr = self._dupv(
                self._DUP_ACQUIRE, ctypes.HRESULT,
                (ctypes.c_uint, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)),
                int(timeout_ms), ctypes.cast(frame_info, ctypes.c_void_p),
                ctypes.byref(res),
            )
        except OSError as exc:
            if exc.winerror == -2005270489:  # DXGI_ERROR_WAIT_TIMEOUT (signed form)
                return self._last_img  # idle desktop: the last frame is current
            raise
        if hr:
            code = hr & 0xFFFFFFFF
            if code == self._DXGI_ERROR_WAIT_TIMEOUT:
                return self._last_img
            if code == 0x887A0026:  # DXGI_ERROR_SESSION_DISCONNECTED (e.g. lock screen)
                raise OSError("DXGI session disconnected")
            raise OSError(f"AcquireNextFrame hr=0x{code:08X}")
        img = None
        try:
            img = self._readback(res)
        finally:
            self._dupv(self._DUP_RELEASE_FRAME, ctypes.HRESULT, ())
        if img is not None:
            self._last_img = img
        return img

    def _readback(self, res):
        import ctypes

        texture = ctypes.c_void_p()
        # R-6a FIX (dangling-IID): local binding — see the matching comment at the
        # IDXGIOutput1 QI. An inline from_buffer_copy inside byref-inside-cast can be
        # collected mid-call; the dangling IID yields E_NOINTERFACE on live grabs.
        iid_texture = (ctypes.c_byte * 16).from_buffer_copy(self._IID_ID3D11TEXTURE2D)
        hr = _DxgiDuplicator._com_call(
            res, self._SLOT_QI, ctypes.HRESULT,
            (ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)),
            ctypes.cast(ctypes.byref(iid_texture), ctypes.c_void_p),
            ctypes.byref(texture),
        )
        if hr:
            raise OSError(f"QI ID3D11Texture2D hr=0x{hr & 0xFFFFFFFF:08X}")
        ctx_call = _DxgiDuplicator._com_caller(self._context)
        ctx_call(self._CTX_COPY_RESOURCE, None,
                 (ctypes.c_void_p, ctypes.c_void_p), self._stage, texture)
        mapped = ctypes.create_string_buffer(24)  # D3D11_MAPPED_SUBRESOURCE
        hr = ctx_call(
            self._CTX_MAP, ctypes.HRESULT,
            (ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p),
            self._stage, 0, 1, 0, ctypes.cast(mapped, ctypes.c_void_p),
        )
        if hr:
            raise OSError(f"Map(staging) hr=0x{hr & 0xFFFFFFFF:08X}")
        try:
            data = int.from_bytes(mapped.raw[0:8], "little")
            pitch = int.from_bytes(mapped.raw[8:12], "little")
            if not data or pitch <= 0:
                raise OSError("Map returned null data")
            width, height = self.width, self.height
            row = width * 4
            view = (ctypes.c_ubyte * (pitch * height)).from_address(data)
            if pitch == row:
                packed = bytes(view)
            else:
                arr = bytearray(row * height)
                mv = memoryview(view)
                for y in range(height):
                    arr[y * row:(y + 1) * row] = mv[y * pitch:y * pitch + row]
                packed = bytes(arr)
            img = Image.frombuffer("RGB", (width, height), packed, "raw", "BGRX", 0, 1)
            # R-7: the packed readback is the pixel source of record for this capture —
            # stash it on the frame so the verification diff can memcmp identical
            # screens (equal bytes == equal pixels) instead of a full-frame diff.
            img._frame_raw = packed  # noqa: SLF001 - private R-7 stash (see models.py)
            return img
        finally:
            ctx_call(self._CTX_UNMAP, None,
                     (ctypes.c_void_p, ctypes.c_uint), self._stage, 0)

    @staticmethod
    def _com_caller(iface):
        import ctypes

        ptr = ctypes.cast(
            ctypes.cast(iface, ctypes.POINTER(ctypes.c_void_p))[0],
            ctypes.POINTER(ctypes.c_void_p),
        )

        def call(slot, restype, argtypes, *args):
            proto = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)
            return proto(ptr[slot])(iface, *args)

        return call

    @staticmethod
    def _com_call(iface, slot, restype, argtypes, *args):
        return _DxgiDuplicator._com_caller(iface)(slot, restype, argtypes, *args)

    def close(self) -> None:
        import ctypes

        for attr in ("_stage", "_dup", "_output1", "_output", "_adapter",
                     "_context", "_device", "_factory"):
            iface = getattr(self, attr, None)
            if isinstance(iface, ctypes.c_void_p) and iface.value:
                try:
                    _DxgiDuplicator._com_call(iface, self._SLOT_RELEASE,
                                              ctypes.c_ulong, ())
                except Exception:
                    pass
                setattr(self, attr, ctypes.c_void_p())
        self._dupv = None
        self._last_img = None

    __del__ = close


def _raw_payload_key(raw_bgra: bytes, width: int, height: int) -> str:
    """R-6: the deterministic fast payload for encode-skip captures (text sessions).

    sha256 over the capture's raw BGRA bytes: exact pixel identity (collision-free in
    practice; PIL determinism made the historical PNG payload equally a many-to-one
    function of the pixels). The prefix marks the payload as a raw key, never as a
    PNG — an accidental consumer (a decoder, a host, a log reader) fails its PNG magic
    check loudly instead of misreading it as an image.
    """
    return "RAW:" + base64.b64encode(
        hashlib.sha256(raw_bgra).digest()
    ).decode("ascii") + f";{width}x{height}"


def _payload_is_raw_key(payload: str) -> bool:
    """True when the payload is the R-6 fast raw-pixel key (never a PNG)."""
    return isinstance(payload, str) and payload.startswith("RAW:")
