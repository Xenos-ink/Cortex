"""R-6 PROTOTYPE: pure-ctypes DXGI Desktop Duplication capture (no new deps).

BENCHMARK PROTOTYPE — the feasibility probe for CORTEX_CAPTURE=dxgi. It answers
one question with numbers: can Windows DXGI duplication grab a 1920x1080 frame
faster than GDI BitBlt (~40ms on this box), using only ctypes (CreateDXGIFactory1
+ D3D11CreateDevice + IDXGIOutput1::DuplicateOutput + CopyResource to a staging
texture + CPU Map)?

All vtable slots below were verified against the Windows SDK 10.0.18362.0
C-interface Vtbl structs (the C structs list IUnknown explicitly, so the slot
indices are used directly):
  IDXGIFactory1      7 EnumAdapters
  IDXGIAdapter      7 EnumOutputs
  IDXGIOutput1      22 DuplicateOutput (IDXGIOutput slots 0-18 + 19/20/21 + 22)
  IDXGIOutputDuplication  7 GetDesc, 8 AcquireNextFrame, 14 ReleaseFrame
                     (MapDesktopSurface slot 12 exists but this box's duplication
                     reports it unavailable — the staging-texture path is used)
  ID3D11Device      5 CreateTexture2D
  ID3D11DeviceContext  14 Map, 15 Unmap, 47 CopyResource

IIDs are written from the SDK DEFINE_GUID lines (Data1 u32 LE, Data2/Data3 u16
LE, Data4 as-is). The texture is QI'd to ID3D11Texture2D, copied into a
D3D11_USAGE_STAGING texture (CPU_ACCESS_READ), CPU-mapped, and the BGRA rows
are decoded through PIL's BGRX raw decoder — the same decode the mss path uses.
"""
from __future__ import annotations

import ctypes
import struct

DXGI_ERROR_WAIT_TIMEOUT = 0x087A0001

IID_IDXGIFACTORY1 = (ctypes.c_byte * 16).from_buffer_copy(
    0x770AAE78.to_bytes(4, "little") + 0xF26F.to_bytes(2, "little")
    + 0x4DBA.to_bytes(2, "little") + bytes([0xA8, 0x29, 0x25, 0x3C, 0x83, 0xD1, 0xB3, 0x87])
)
IID_IDXGIOUTPUT1 = (ctypes.c_byte * 16).from_buffer_copy(
    0x00CDDEA8.to_bytes(4, "little") + 0x939B.to_bytes(2, "little")
    + 0x4B83.to_bytes(2, "little") + bytes([0xA3, 0x40, 0xA6, 0x85, 0x22, 0x66, 0x66, 0xCC])
)
IID_ID3D11TEXTURE2D = (ctypes.c_byte * 16).from_buffer_copy(
    0x6F15AAF2.to_bytes(4, "little") + 0xD208.to_bytes(2, "little")
    + 0x4E89.to_bytes(2, "little") + bytes([0x9A, 0xB4, 0x48, 0x95, 0x35, 0xD3, 0x4F, 0x9C])
)

# Verified C-vtable slots (see module docstring)
SLOT_ENUM_ADAPTERS = 7        # IDXGIFactory1::EnumAdapters
SLOT_ENUM_OUTPUTS = 7         # IDXGIAdapter::EnumOutputs
SLOT_DUP_OUTPUT = 22          # IDXGIOutput1::DuplicateOutput
DUP_GET_DESC = 7              # IDXGIOutputDuplication::GetDesc
DUP_ACQUIRE = 8               # IDXGIOutputDuplication::AcquireNextFrame
DUP_RELEASE_FRAME = 14        # IDXGIOutputDuplication::ReleaseFrame
DEV_CREATE_TEX2D = 5          # ID3D11Device::CreateTexture2D
CTX_MAP = 14                  # ID3D11DeviceContext::Map
CTX_UNMAP = 15                # ID3D11DeviceContext::Unmap
CTX_COPY_RESOURCE = 47        # ID3D11DeviceContext::CopyResource
SLOT_QI = 0
SLOT_RELEASE = 2

D3D11_USAGE_STAGING = 3
D3D11_CPU_ACCESS_READ = 0x20000
DXGI_FORMAT_B8G8R8A8_UNORM = 87
D3D11_MAP_READ = 1


class _Vtbl:
    __slots__ = ("iface", "ptr")

    def __init__(self, iface: ctypes.c_void_p):
        self.iface = iface
        vtbl_pp = ctypes.cast(iface, ctypes.POINTER(ctypes.c_void_p))
        self.ptr = ctypes.cast(vtbl_pp[0], ctypes.POINTER(ctypes.c_void_p))

    def call(self, slot: int, restype, argtypes, *args):
        proto = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)
        return proto(self.ptr[slot])(self.iface, *args)


def _hr(x: int) -> str:
    return f"hr=0x{x & 0xFFFFFFFF:08X}"


class Duplicator:
    """One duplicated output; grab() returns a PIL RGB Image (or the last frame)."""

    def __init__(self, adapter_index: int = 0, output_index: int = 0) -> None:
        dxgi = ctypes.windll.dxgi
        d3d11 = ctypes.windll.d3d11

        factory = ctypes.c_void_p()
        hr = dxgi.CreateDXGIFactory1(
            ctypes.byref(IID_IDXGIFACTORY1), ctypes.byref(factory)
        )
        if hr:
            raise OSError(f"CreateDXGIFactory1 {_hr(hr)}")
        self._factory = factory

        adapter = ctypes.c_void_p()
        hr = _Vtbl(factory).call(
            SLOT_ENUM_ADAPTERS, ctypes.HRESULT,
            (ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p)),
            adapter_index, ctypes.byref(adapter),
        )
        if hr:
            raise OSError(f"EnumAdapters {_hr(hr)}")
        self._adapter = adapter

        output = ctypes.c_void_p()
        hr = _Vtbl(adapter).call(
            SLOT_ENUM_OUTPUTS, ctypes.HRESULT,
            (ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p)),
            output_index, ctypes.byref(output),
        )
        if hr:
            raise OSError(f"EnumOutputs {_hr(hr)}")
        self._output = output

        output1 = ctypes.c_void_p()
        hr = _Vtbl(output).call(
            SLOT_QI, ctypes.HRESULT,
            (ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)),
            ctypes.cast(ctypes.byref(IID_IDXGIOUTPUT1), ctypes.c_void_p),
            ctypes.byref(output1),
        )
        if hr:
            raise OSError(f"QI IDXGIOutput1 {_hr(hr)}")
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
        # NULL adapter + D3D_DRIVER_TYPE_HARDWARE(1) + SDK 7
        hr = create_device(d3d11.D3D11CreateDevice)(
            None, 1, None, 0, None, 0, 7,
            ctypes.byref(device), ctypes.byref(feature_level), ctypes.byref(context),
        )
        if hr:
            raise OSError(f"D3D11CreateDevice {_hr(hr)}")
        self._device = device
        self._context = context

        dup = ctypes.c_void_p()
        hr = _Vtbl(output1).call(
            SLOT_DUP_OUTPUT, ctypes.HRESULT,
            (ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)),
            device, ctypes.byref(dup),
        )
        if hr:
            raise OSError(f"DuplicateOutput {_hr(hr)} (protected/RDP session?)")
        self._dup = dup
        self._dupv = _Vtbl(dup)

        # DXGI_OUTDUPL_DESC {DXGI_MODE_DESC ModeDesc(24B); Rotation(4); BOOL(4)}
        desc = ctypes.create_string_buffer(40)
        self._dupv.call(DUP_GET_DESC, None, (ctypes.c_void_p,),
                        ctypes.cast(desc, ctypes.c_void_p))
        raw = desc.raw
        self.width = int.from_bytes(raw[0:4], "little")
        self.height = int.from_bytes(raw[4:8], "little")
        self.rotation = int.from_bytes(raw[28:32], "little")
        self.fmt_name = "B8G8R8A8"

        # One staging texture, reused across grabs (GPU->CPU readback surface).
        stage_desc = struct.pack(
            "<11I",
            self.width, self.height, 1, 1,
            DXGI_FORMAT_B8G8R8A8_UNORM, 1, 0,
            D3D11_USAGE_STAGING, 0, D3D11_CPU_ACCESS_READ, 0,
        )
        stage = ctypes.c_void_p()
        hr = _Vtbl(device).call(
            DEV_CREATE_TEX2D, ctypes.HRESULT,
            (ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)),
            ctypes.cast(ctypes.create_string_buffer(stage_desc), ctypes.c_void_p),
            None, ctypes.byref(stage),
        )
        if hr:
            raise OSError(f"CreateTexture2D(staging) {_hr(hr)}")
        self._stage = stage
        self._last_img = None

    def grab(self, timeout_ms: int = 8):
        """One frame as a PIL RGB Image.

        Desktop Duplication only presents a NEW frame when the screen changed;
        on DXGI_ERROR_WAIT_TIMEOUT the previously acquired frame is still the
        current desktop image, so it is returned again (snapshot parity with a
        GDI BitBlt up to in-flight animation).
        """
        frame_info = ctypes.create_string_buffer(64)
        res = ctypes.c_void_p()
        try:
            hr = self._dupv.call(
                DUP_ACQUIRE, ctypes.HRESULT,
                (ctypes.c_uint, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)),
                timeout_ms, ctypes.cast(frame_info, ctypes.c_void_p),
                ctypes.byref(res),
            )
        except OSError as exc:
            if exc.winerror == -2005270489:  # DXGI_ERROR_WAIT_TIMEOUT (signed)
                return self._last_img  # desktop idle: the last frame is current
            raise
        code = hr & 0xFFFFFFFF
        if code == DXGI_ERROR_WAIT_TIMEOUT:
            return self._last_img
        if code == 0x887A0027:  # DXGI_ERROR_INVALID_CALL — no frame to release
            return self._last_img
        if hr:
            raise OSError(f"AcquireNextFrame {_hr(hr)}")
        img = None
        try:
            img = self._readback(res)
        finally:
            self._dupv.call(DUP_RELEASE_FRAME, ctypes.HRESULT, ())
        if img is not None:
            self._last_img = img
        return img

    def _readback(self, res: ctypes.c_void_p):
        texture = ctypes.c_void_p()
        hr = _Vtbl(res).call(
            SLOT_QI, ctypes.HRESULT,
            (ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)),
            ctypes.cast(ctypes.byref(IID_ID3D11TEXTURE2D), ctypes.c_void_p),
            ctypes.byref(texture),
        )
        if hr:
            raise OSError(f"QI ID3D11Texture2D {_hr(hr)}")
        ctx = _Vtbl(self._context)
        ctx.call(CTX_COPY_RESOURCE, None, (ctypes.c_void_p, ctypes.c_void_p),
                 self._stage, texture)
        mapped = ctypes.create_string_buffer(24)  # D3D11_MAPPED_SUBRESOURCE
        hr = ctx.call(
            CTX_MAP, ctypes.HRESULT,
            (ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p),
            self._stage, 0, D3D11_MAP_READ, 0, ctypes.cast(mapped, ctypes.c_void_p),
        )
        if hr:
            raise OSError(f"Map(staging) {_hr(hr)}")
        try:
            data = int.from_bytes(mapped.raw[0:8], "little")
            pitch = int.from_bytes(mapped.raw[8:12], "little")
            if not data:
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
            from PIL import Image
            return Image.frombuffer("RGB", (width, height), packed, "raw", "BGRX", 0, 1)
        finally:
            ctx.call(CTX_UNMAP, None, (ctypes.c_void_p, ctypes.c_uint),
                     self._stage, 0)

    def close(self) -> None:
        for attr in ("_stage", "_dup", "_output1", "_output", "_adapter",
                     "_context", "_device", "_factory"):
            iface = getattr(self, attr, None)
            if isinstance(iface, ctypes.c_void_p) and iface.value:
                try:
                    _Vtbl(iface).call(SLOT_RELEASE, ctypes.c_ulong, ())
                except Exception:
                    pass
                setattr(self, attr, ctypes.c_void_p())
        self._dupv = None

    __del__ = close
