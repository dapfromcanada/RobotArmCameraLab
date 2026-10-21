"""Tests for two_d.processing."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import cv2
import numpy as np
import pytest

from camera_lab.two_d import processing
from camera_lab.two_d.models import ROI, CameraSettings, Frame
from camera_lab.two_d.processing import (
    PROCESSORS,
    demosaic,
    encode_jpeg,
    frame_is_colour,
    make_preview,
    to_float01,
    to_luma,
)


def _frame(data: np.ndarray, fmt: str, bits: int = 8) -> Frame:
    h, w = data.shape[:2]
    return Frame(
        data=data,
        pixel_format=fmt,
        bit_depth=bits,
        timestamp_utc=datetime(2026, 10, 21, tzinfo=UTC),
        frame_id=1,
        settings=CameraSettings(
            exposure_us=1, gain_db=0, pixel_format=fmt, roi=ROI(x=0, y=0, width=w, height=h)
        ),
    )


def _mosaic_of_red(pattern: str, value: int = 200, dtype: Any = np.uint8) -> np.ndarray:
    """A mosaic of a pure-red scene for a GenICam pattern name (e.g. 'RG')."""
    m = np.zeros((8, 8), dtype)
    layout = {"RG": (0, 0), "BG": (1, 1), "GR": (0, 1), "GB": (1, 0)}
    r, c = layout[pattern]
    m[r::2, c::2] = value
    return m


@pytest.mark.parametrize("pattern", ["RG", "BG", "GR", "GB"])
def test_demosaic_pattern_mapping(pattern: str) -> None:
    rgb = demosaic(_mosaic_of_red(pattern), f"Bayer{pattern}8")
    centre = rgb[2:6, 2:6].reshape(-1, 3).mean(axis=0)
    assert centre[0] == pytest.approx(200) and centre[1] == 0 and centre[2] == 0


def test_demosaic_uint16_and_passthrough() -> None:
    rgb = demosaic(_mosaic_of_red("RG", 4000, np.uint16), "BayerRG12")
    assert rgb.dtype == np.uint16 and rgb[3, 3, 0] == 4000
    mono = np.zeros((4, 4), np.uint8)
    assert demosaic(mono, "Mono8") is mono
    with pytest.raises(ValueError):
        demosaic(mono, "BayerXY8")


def test_to_float01_and_luma() -> None:
    assert to_float01(np.array([0, 4095], np.uint16), 12).tolist() == [0.0, 1.0]
    rgb = np.array([[[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]])
    assert to_luma(rgb)[0].tolist() == pytest.approx([0.2126, 0.7152, 0.0722])
    assert to_luma(np.ones((2, 2))).shape == (2, 2)


def test_make_preview_shapes() -> None:
    big = _frame(np.full((1536, 2048), 255, np.uint8), "BayerRG8")
    p = make_preview(big)
    assert p.shape == (768, 1024, 3) and p.dtype == np.uint8
    small = _frame(np.full((10, 20), 4095, np.uint16), "Mono12", 12)
    ps = make_preview(small, long_edge=1024)
    assert ps.shape == (10, 20, 3) and int(ps.max()) == 255
    rgb = _frame(np.zeros((40, 80, 3), np.uint8), "RGB8")
    assert make_preview(rgb, long_edge=20).shape == (10, 20, 3)
    with pytest.raises(ValueError):
        make_preview(rgb, long_edge=0)


def test_encode_jpeg_round_trip() -> None:
    img = np.zeros((16, 16, 3), np.uint8)
    img[..., 0] = 255
    decoded = cv2.imdecode(np.frombuffer(encode_jpeg(img), np.uint8), cv2.IMREAD_COLOR)
    assert decoded[..., 2].mean() > 240  # red is last in OpenCV's BGR
    assert encode_jpeg(np.zeros((4, 4), np.uint8))[:2] == b"\xff\xd8"
    with pytest.raises(TypeError):
        encode_jpeg(np.zeros((4, 4), np.uint16))  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        encode_jpeg(img, quality=0)


def test_registry_and_helpers() -> None:
    assert PROCESSORS.names() == [
        "demosaic",
        "encode_jpeg",
        "make_preview",
        "to_float01",
        "to_luma",
    ]
    assert PROCESSORS.get("to_luma") is processing.to_luma
    assert frame_is_colour(_frame(np.zeros((4, 4), np.uint8), "BayerRG8"))
    assert not frame_is_colour(_frame(np.zeros((4, 4), np.uint8), "Mono8"))
