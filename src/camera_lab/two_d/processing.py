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
#: Previews of formats deeper than this are linear HDR and get a display tone curve.
PREVIEW_LINEAR_MAX_BITS = 16
HDR_DISPLAY_PERCENTILE = 99.5
HDR_DISPLAY_GAMMA = 1 / 2.2


def hdr_display(img: npt.NDArray[np.float64]) -> npt.NDArray[np.float64]:
    """Display tone curve for linear HDR (preview only, never the stored data):
    scale so the 99.5th percentile is white, then a 1/2.2 display gamma."""
    top = float(np.percentile(img, HDR_DISPLAY_PERCENTILE))
    out: npt.NDArray[np.float64] = np.clip(img / max(top, 1e-12), 0.0, 1.0) ** HDR_DISPLAY_GAMMA
    return out


def demosaic(data: npt.NDArray[Any], pixel_format: str) -> npt.NDArray[Any]:
    """Bayer (HxW) → RGB (HxWx3), same dtype and code range. Non-Bayer data is returned as is."""
    if not is_bayer(pixel_format):
        return data
    code = _BAYER_TO_CV.get(pixel_format[:7])
    if code is None:
        raise ValueError(f"unknown Bayer pattern {pixel_format!r}")
    if data.dtype == np.uint32:  # 24-bit HDR: OpenCV demosaics only 8/16-bit
        return _bilinear_demosaic(data, pixel_format[:7])
    out: npt.NDArray[Any] = np.asarray(cv2.cvtColor(np.ascontiguousarray(data), code))
    return out


#: (row, col) of R and B in the 2 × 2 Bayer cell; G fills the other two sites.
_BAYER_SITES = {
    "BayerRG": ((0, 0), (1, 1)),
    "BayerBG": ((1, 1), (0, 0)),
    "BayerGR": ((0, 1), (1, 0)),
    "BayerGB": ((1, 0), (0, 1)),
}
_K_G = np.array([[0, 1, 0], [1, 4, 1], [0, 1, 0]], np.float64) / 4.0
_K_RB = np.array([[1, 2, 1], [2, 4, 2], [1, 2, 1]], np.float64) / 4.0


def _bilinear_demosaic(data: npt.NDArray[Any], pattern: str) -> npt.NDArray[np.uint32]:
    """Bilinear demosaic in float64 (exact for 24-bit codes) → uint32 RGB."""
    raw = np.asarray(data, np.float64)
    (ry, rx), (by, bx) = _BAYER_SITES[pattern]
    r = np.zeros_like(raw)
    b = np.zeros_like(raw)
    r[ry::2, rx::2] = raw[ry::2, rx::2]
    b[by::2, bx::2] = raw[by::2, bx::2]
    g = raw - r - b
    planes = [cv2.filter2D(r, -1, _K_RB), cv2.filter2D(g, -1, _K_G), cv2.filter2D(b, -1, _K_RB)]
    rgb = np.stack(planes, axis=2)
    out: npt.NDArray[np.uint32] = np.rint(np.clip(rgb, 0, None)).astype(np.uint32)
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
    if frame.bit_depth > PREVIEW_LINEAR_MAX_BITS:  # linear HDR: tone-map for the eye
        img = hdr_display(img)
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
