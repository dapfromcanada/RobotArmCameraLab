"""Image processors: a registry plus the built-ins scoring and previews need.

Add a processor with ``@PROCESSORS.register("name")``; nothing else changes.

Bayer naming (important): GenICam names a Bayer pattern by its top-left 2×2
block (``BayerRG`` = R G / G B). OpenCV names its ``COLOR_Bayer??2RGB`` codes
from the second row/column, so the codes are swapped:

=============  ===========================
GenICam        OpenCV code
=============  ===========================
``BayerRG*``   ``cv2.COLOR_BayerBG2RGB``
``BayerBG*``   ``cv2.COLOR_BayerRG2RGB``
``BayerGR*``   ``cv2.COLOR_BayerGB2RGB``
``BayerGB*``   ``cv2.COLOR_BayerGR2RGB``
=============  ===========================

This mapping is verified by a test that demosaics a pure-red RGGB mosaic.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import cv2
import numpy as np
import numpy.typing as npt

from camera_lab.core.registry import Registry
from camera_lab.two_d.models import Frame, is_bayer, is_rgb

PROCESSORS: Registry[Callable[..., Any]] = Registry("image processor")

_BAYER_TO_CV = {
    "BayerRG": cv2.COLOR_BayerBG2RGB,
    "BayerBG": cv2.COLOR_BayerRG2RGB,
    "BayerGR": cv2.COLOR_BayerGB2RGB,
    "BayerGB": cv2.COLOR_BayerGR2RGB,
}
REC709 = (0.2126, 0.7152, 0.0722)


def demosaic(data: npt.NDArray[Any], pixel_format: str) -> npt.NDArray[Any]:
    """Bayer (HxW) → RGB (HxWx3), same dtype and code range. Non-Bayer data is returned as is."""
    if not is_bayer(pixel_format):
        return data
    code = _BAYER_TO_CV.get(pixel_format[:7])
    if code is None:
        raise ValueError(f"unknown Bayer pattern {pixel_format!r}")
    out: npt.NDArray[Any] = np.asarray(cv2.cvtColor(np.ascontiguousarray(data), code))
    return out


def to_display_array(frame: Frame) -> npt.NDArray[Any]:
    """Return integer data as Mono (HxW) or RGB (HxWx3): Bayer is demosaiced."""
    return demosaic(frame.data, frame.pixel_format)


def to_float01(data: npt.NDArray[Any], bit_depth: int) -> npt.NDArray[np.float64]:
    """Integer codes → float64 in 0–1 (divide by ``2**bit_depth - 1``)."""
    out: npt.NDArray[np.float64] = data.astype(np.float64) / float((1 << bit_depth) - 1)
    return out


def to_luma(img: npt.NDArray[Any]) -> npt.NDArray[np.float64]:
    """RGB (HxWx3) → luma with Rec. 709 weights. Mono (HxW) is returned as float64."""
    arr = img.astype(np.float64, copy=False)
    if arr.ndim == 2:
        return arr
    out: npt.NDArray[np.float64] = arr @ np.array(REC709)
    return out


def make_preview(frame: Frame, long_edge: int = 1024) -> npt.NDArray[np.uint8]:
    """Return an 8-bit RGB preview (HxWx3), long edge ≤ ``long_edge`` (no upscaling)."""
    if long_edge < 1:
        raise ValueError("long_edge must be >= 1")
    img = to_float01(to_display_array(frame), frame.bit_depth)
    rgb = np.repeat(img[..., None], 3, axis=2) if img.ndim == 2 else img
    h, w = rgb.shape[:2]
    scale = long_edge / max(h, w)
    if scale < 1.0:
        size = (max(1, round(w * scale)), max(1, round(h * scale)))
        rgb = cv2.resize(rgb, size, interpolation=cv2.INTER_AREA)
    out: npt.NDArray[np.uint8] = np.rint(np.clip(rgb, 0.0, 1.0) * 255.0).astype(np.uint8)
    return out


def encode_jpeg(img: npt.NDArray[np.uint8], quality: int = 85) -> bytes:
    """Encode an 8-bit RGB (HxWx3) or grey (HxW) image as JPEG bytes."""
    if img.dtype != np.uint8:
        raise TypeError("encode_jpeg needs uint8 data")
    if not 1 <= quality <= 100:
        raise ValueError("quality must be 1..100")
    bgr = cv2.cvtColor(img, cv2.COLOR_RGB2BGR) if img.ndim == 3 else img
    ok, buf = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise RuntimeError("JPEG encoding failed")
    return bytes(buf.tobytes())


# Registered after definition (not as decorators) so the functions keep their types.
PROCESSORS.register("demosaic")(demosaic)
PROCESSORS.register("to_float01")(to_float01)
PROCESSORS.register("to_luma")(to_luma)
PROCESSORS.register("make_preview")(make_preview)
PROCESSORS.register("encode_jpeg")(encode_jpeg)


def frame_is_colour(frame: Frame) -> bool:
    """True for Bayer and RGB formats."""
    return is_bayer(frame.pixel_format) or is_rgb(frame.pixel_format)
