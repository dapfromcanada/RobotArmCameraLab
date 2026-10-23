"""Unpack GenICam PFNC LSB-packed pixel data (``*10p``, ``*12p``) to uint16.

PFNC "p" formats pack pixels back to back, least-significant bit first:

- 12p: 2 pixels in 3 bytes: ``p0 = b0 | (b1 & 0x0F) << 8``, ``p1 = b1 >> 4 | b2 << 4``
- 10p: 4 pixels in 5 bytes: ``p0 = b0 | (b1 & 0x03) << 8``,
  ``p1 = b1 >> 2 | (b2 & 0x0F) << 6``, ``p2 = b2 >> 4 | (b3 & 0x3F) << 4``,
  ``p3 = b3 >> 6 | b4 << 2``

The output holds the camera's native values (0–1023 / 0–4095), not rescaled.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import numpy.typing as npt


def unpack_12p(data: npt.NDArray[np.uint8], count: int) -> npt.NDArray[np.uint16]:
    """Unpack ``count`` 12-bit pixels from PFNC 12p bytes."""
    if count % 2:
        raise ValueError("12p needs an even pixel count")
    need = count * 3 // 2
    if data.size < need:
        raise ValueError(f"12p payload too short: {data.size} < {need} bytes")
    b = data[:need].astype(np.uint16).reshape(-1, 3)
    out = np.empty((b.shape[0], 2), np.uint16)
    out[:, 0] = b[:, 0] | ((b[:, 1] & 0x0F) << 8)
    out[:, 1] = (b[:, 1] >> 4) | (b[:, 2] << 4)
    return out.reshape(-1)


def unpack_10p(data: npt.NDArray[np.uint8], count: int) -> npt.NDArray[np.uint16]:
    """Unpack ``count`` 10-bit pixels from PFNC 10p bytes."""
    if count % 4:
        raise ValueError("10p needs a pixel count divisible by 4")
    need = count * 5 // 4
    if data.size < need:
        raise ValueError(f"10p payload too short: {data.size} < {need} bytes")
    b = data[:need].astype(np.uint16).reshape(-1, 5)
    out = np.empty((b.shape[0], 4), np.uint16)
    out[:, 0] = b[:, 0] | ((b[:, 1] & 0x03) << 8)
    out[:, 1] = (b[:, 1] >> 2) | ((b[:, 2] & 0x0F) << 6)
    out[:, 2] = (b[:, 2] >> 4) | ((b[:, 3] & 0x3F) << 4)
    out[:, 3] = (b[:, 3] >> 6) | (b[:, 4] << 2)
    return out.reshape(-1)


def pack_12p(pixels: npt.NDArray[Any]) -> npt.NDArray[np.uint8]:
    """Inverse of :func:`unpack_12p` (used by tests and fake cameras)."""
    p = pixels.astype(np.uint16).reshape(-1, 2)
    out = np.empty((p.shape[0], 3), np.uint8)
    out[:, 0] = p[:, 0] & 0xFF
    out[:, 1] = ((p[:, 0] >> 8) & 0x0F) | ((p[:, 1] & 0x0F) << 4)
    out[:, 2] = (p[:, 1] >> 4) & 0xFF
    return out.reshape(-1)


def pack_10p(pixels: npt.NDArray[Any]) -> npt.NDArray[np.uint8]:
    """Inverse of :func:`unpack_10p` (used by tests and fake cameras)."""
    p = pixels.astype(np.uint16).reshape(-1, 4)
    out = np.empty((p.shape[0], 5), np.uint8)
    out[:, 0] = p[:, 0] & 0xFF
    out[:, 1] = ((p[:, 0] >> 8) & 0x03) | ((p[:, 1] & 0x3F) << 2)
    out[:, 2] = ((p[:, 1] >> 6) & 0x0F) | ((p[:, 2] & 0x0F) << 4)
    out[:, 3] = ((p[:, 2] >> 4) & 0x3F) | ((p[:, 3] & 0x03) << 6)
    out[:, 4] = (p[:, 3] >> 2) & 0xFF
    return out.reshape(-1)


# ---------------------------------------------------------------------------
# JAI Apex RGB formats. Layouts verified bit-exactly against the camera's test
# patterns (HorizontalColorBar, GreyHorizontalRamp) on the AP-3200T-PGE,
# 2026-10-21: each decoded value >> (bits - 8) equals the RGB8 frame.
#
# RGB10p32 (PFNC):    one little-endian 32-bit word per pixel:
#                     R = w[9:0], G = w[19:10], B = w[29:20]
# RGB10V1Packed (GEV): 4 bytes per pixel: b0 = R[1:0] | G[1:0] << 2 | B[1:0] << 4,
#                     b1 = B[9:2], b2 = G[9:2], b3 = R[9:2]
# RGB12V1Packed (GEV): components R, G, B, R, ... packed 2 per 3 bytes:
#                     v0 = b0 << 4 | b1 & 0xF, v1 = b2 << 4 | b1 >> 4
# ---------------------------------------------------------------------------


def unpack_rgb10p32(data: npt.NDArray[np.uint8], pixels: int) -> npt.NDArray[np.uint16]:
    """Unpack ``pixels`` RGB10p32 pixels to an (N, 3) uint16 array."""
    need = pixels * 4
    if data.size < need:
        raise ValueError(f"RGB10p32 payload too short: {data.size} < {need} bytes")
    w = np.frombuffer(data[:need].tobytes(), dtype="<u4")
    out = np.empty((pixels, 3), np.uint16)
    out[:, 0] = w & 0x3FF
    out[:, 1] = (w >> 10) & 0x3FF
    out[:, 2] = (w >> 20) & 0x3FF
    return out


def unpack_rgb10v1(data: npt.NDArray[np.uint8], pixels: int) -> npt.NDArray[np.uint16]:
    """Unpack ``pixels`` RGB10V1Packed pixels to an (N, 3) uint16 array."""
    need = pixels * 4
    if data.size < need:
        raise ValueError(f"RGB10V1Packed payload too short: {data.size} < {need} bytes")
    b = data[:need].astype(np.uint16).reshape(-1, 4)
    out = np.empty((pixels, 3), np.uint16)
    out[:, 0] = (b[:, 3] << 2) | (b[:, 0] & 0x3)
    out[:, 1] = (b[:, 2] << 2) | ((b[:, 0] >> 2) & 0x3)
    out[:, 2] = (b[:, 1] << 2) | ((b[:, 0] >> 4) & 0x3)
    return out


def unpack_rgb12v1(data: npt.NDArray[np.uint8], pixels: int) -> npt.NDArray[np.uint16]:
    """Unpack ``pixels`` RGB12V1Packed pixels to an (N, 3) uint16 array."""
    values = pixels * 3
    if values % 2:
        raise ValueError("RGB12V1Packed needs an even number of components")
    need = values * 3 // 2
    if data.size < need:
        raise ValueError(f"RGB12V1Packed payload too short: {data.size} < {need} bytes")
    t = data[:need].astype(np.uint16).reshape(-1, 3)
    out = np.empty((t.shape[0], 2), np.uint16)
    out[:, 0] = (t[:, 0] << 4) | (t[:, 1] & 0xF)
    out[:, 1] = (t[:, 2] << 4) | (t[:, 1] >> 4)
    return out.reshape(-1, 3)


def pack_rgb10p32(rgb: npt.NDArray[Any]) -> npt.NDArray[np.uint8]:
    """Inverse of :func:`unpack_rgb10p32` (tests / fakes)."""
    p = rgb.astype(np.uint32).reshape(-1, 3)
    w = (p[:, 0] & 0x3FF) | ((p[:, 1] & 0x3FF) << 10) | ((p[:, 2] & 0x3FF) << 20)
    return np.frombuffer(w.astype("<u4").tobytes(), np.uint8).copy()


def pack_rgb10v1(rgb: npt.NDArray[Any]) -> npt.NDArray[np.uint8]:
    """Inverse of :func:`unpack_rgb10v1` (tests / fakes)."""
    p = rgb.astype(np.uint16).reshape(-1, 3)
    out = np.empty((p.shape[0], 4), np.uint8)
    out[:, 0] = (p[:, 0] & 3) | ((p[:, 1] & 3) << 2) | ((p[:, 2] & 3) << 4)
    out[:, 1] = p[:, 2] >> 2
    out[:, 2] = p[:, 1] >> 2
    out[:, 3] = p[:, 0] >> 2
    return out.reshape(-1)


def pack_rgb12v1(rgb: npt.NDArray[Any]) -> npt.NDArray[np.uint8]:
    """Inverse of :func:`unpack_rgb12v1` (tests / fakes)."""
    v = rgb.astype(np.uint16).reshape(-1, 2)
    out = np.empty((v.shape[0], 3), np.uint8)
    out[:, 0] = v[:, 0] >> 4
    out[:, 1] = (v[:, 0] & 0xF) | ((v[:, 1] & 0xF) << 4)
    out[:, 2] = v[:, 1] >> 4
    return out.reshape(-1)


def unpack_24(data: npt.NDArray[np.uint8], count: int) -> npt.NDArray[np.uint32]:
    """Unpack ``count`` 24-bit pixels (3 bytes, little-endian; Lucid Mono24/BayerRG24)."""
    if data.size < count * 3:
        raise ValueError(f"24-bit payload has {data.size} bytes, need {count * 3}")
    t = data[: count * 3].reshape(count, 3).astype(np.uint32)
    out = np.asarray(t[:, 0] | (t[:, 1] << 8) | (t[:, 2] << 16), dtype=np.uint32)
    return out
