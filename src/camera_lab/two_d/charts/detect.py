"""Find library test charts in an image (Phase 10 step 2).

AprilTag 36h11 tags with ids 100–199 are detected on an 8-bit luma copy of the
frame (Bayer demosaiced, contrast-stretched, downscaled when the sensor is
large), mapped to their charts through the library, and a homography from
chart millimetres to image pixels is fitted to the tag corners. Tags outside
100–199 are ignored (other apps' targets); tags in the range that no chart owns
are reported as unknown.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import cv2
import numpy as np
import numpy.typing as npt
from pydantic import BaseModel, ConfigDict

from camera_lab.two_d.charts.models import TAG_ID_MAX, TAG_ID_MIN, ChartSpec, TagSpec
from camera_lab.two_d.models import Frame
from camera_lab.two_d.processing import demosaic

#: Detection runs on a copy whose long edge is at most this (corners are scaled back).
DETECT_LONG_EDGE = 2048
#: Percentiles for the contrast stretch (dark 12-bit frames still detect).
STRETCH_LO_PCT = 0.5
STRETCH_HI_PCT = 99.5


class TagHit(BaseModel):
    """One detected tag: corners in full-image pixels, AprilTag order (tl, tr, br, bl)."""

    model_config = ConfigDict(frozen=True)

    id: int
    corners_px: tuple[tuple[float, float], ...]

    @property
    def side_px(self) -> float:
        c = np.array(self.corners_px)
        return float(np.mean([np.linalg.norm(c[k] - c[(k + 1) % 4]) for k in range(4)]))


class ChartDetection(BaseModel):
    """A library chart seen in the image."""

    model_config = ConfigDict(frozen=True)

    chart_id: str
    version: int
    type: str
    tags: tuple[TagHit, ...]
    #: 3 × 3 homography (row-major) from chart mm (page frame) to image pixels.
    homography: tuple[float, ...]
    #: Mean tag side in pixels / tag side in mm (scale at the chart).
    px_per_mm: float

    def to_record(self) -> dict[str, Any]:
        """Compact JSON for capture sidecars and tool output."""
        return {
            "chart_id": self.chart_id,
            "version": self.version,
            "type": self.type,
            "tag_ids": [t.id for t in self.tags],
            "px_per_mm": round(self.px_per_mm, 4),
            "homography": [round(v, 6) for v in self.homography],
            "tag_corners_px": {
                str(t.id): [[round(x, 2), round(y, 2)] for x, y in t.corners_px] for t in self.tags
            },
        }


class ChartScan(BaseModel):
    """Everything found in one image."""

    model_config = ConfigDict(frozen=True)

    charts: tuple[ChartDetection, ...] = ()
    unknown_tags: tuple[int, ...] = ()

    def to_record(self) -> list[dict[str, Any]]:
        out = [c.to_record() for c in self.charts]
        out += [
            {"chart_id": None, "tag_ids": [t], "note": "tag not in the chart library"}
            for t in self.unknown_tags
        ]
        return out


def _luma8(data: npt.NDArray[Any], pixel_format: str) -> npt.NDArray[np.uint8]:
    img = demosaic(data, pixel_format)
    if img.ndim == 3:
        img = img.astype(np.float32) @ np.array([0.2126, 0.7152, 0.0722], np.float32)
    arr = img.astype(np.float32, copy=False)
    lo, hi = np.percentile(arr[::4, ::4], (STRETCH_LO_PCT, STRETCH_HI_PCT))
    if hi <= lo:
        hi = lo + 1.0
    out: npt.NDArray[np.uint8] = np.clip((arr - lo) * (255.0 / (hi - lo)), 0, 255).astype(np.uint8)
    return out


def _detector() -> cv2.aruco.ArucoDetector:
    params = cv2.aruco.DetectorParameters()
    params.cornerRefinementMethod = cv2.aruco.CORNER_REFINE_SUBPIX
    dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_APRILTAG_36h11)
    return cv2.aruco.ArucoDetector(dictionary, params)


def detect_tags(data: npt.NDArray[Any], pixel_format: str = "Mono8") -> list[TagHit]:
    """AprilTag 36h11 tags with ids 100–199, corners in full-image pixels."""
    luma = _luma8(data, pixel_format)
    h, w = luma.shape
    scale = min(1.0, DETECT_LONG_EDGE / max(h, w))
    small = (
        luma
        if scale == 1.0
        else cv2.resize(luma, (round(w * scale), round(h * scale)), interpolation=cv2.INTER_AREA)
    )
    corners, ids, _ = _detector().detectMarkers(small)
    hits = []
    if ids is not None:
        for c, i in zip(corners, ids.ravel(), strict=True):
            if TAG_ID_MIN <= int(i) <= TAG_ID_MAX:
                pts = tuple((float(x) / scale, float(y) / scale) for x, y in c[0])
                hits.append(TagHit(id=int(i), corners_px=pts))
    return sorted(hits, key=lambda t: t.id)


def _tag_corners_mm(t: TagSpec) -> list[tuple[float, float]]:
    s = t.size_mm
    return [(t.x_mm, t.y_mm), (t.x_mm + s, t.y_mm), (t.x_mm + s, t.y_mm + s), (t.x_mm, t.y_mm + s)]


def charts_from_tags(hits: list[TagHit], library: Mapping[int, ChartSpec]) -> ChartScan:
    """Group tag hits by chart and fit chart-mm → pixel homographies."""
    by_chart: dict[str, list[TagHit]] = {}
    unknown = []
    for hit in hits:
        chart = library.get(hit.id)
        if chart is None:
            unknown.append(hit.id)
        else:
            by_chart.setdefault(chart.id, []).append(hit)
    found = []
    for chart_id, chart_hits in sorted(by_chart.items()):
        chart = library[chart_hits[0].id]
        specs = {t.id: t for t in chart.tags}
        src = np.array([p for h in chart_hits for p in _tag_corners_mm(specs[h.id])], np.float64)
        dst = np.array([p for h in chart_hits for p in h.corners_px], np.float64)
        hom, _ = cv2.findHomography(src, dst, 0)
        if hom is None:  # degenerate corners
            continue
        hom = hom / hom[2, 2]
        px_per_mm = float(np.mean([h.side_px / specs[h.id].size_mm for h in chart_hits]))
        found.append(
            ChartDetection(
                chart_id=chart_id,
                version=chart.version,
                type=chart.type,
                tags=tuple(chart_hits),
                homography=tuple(float(v) for v in hom.ravel()),
                px_per_mm=px_per_mm,
            )
        )
    return ChartScan(charts=tuple(found), unknown_tags=tuple(sorted(unknown)))


def detect_charts(frame: Frame, library: Mapping[int, ChartSpec]) -> ChartScan:
    """Find the library charts in ``frame``."""
    return charts_from_tags(detect_tags(frame.data, frame.pixel_format), library)


def chart_to_image(det: ChartDetection, points_mm: npt.ArrayLike) -> npt.NDArray[np.float64]:
    """Map chart points (mm, page frame) to image pixels with the detection's homography."""
    pts = np.asarray(points_mm, np.float64).reshape(-1, 1, 2)
    hom = np.array(det.homography, np.float64).reshape(3, 3)
    out = np.asarray(cv2.perspectiveTransform(pts, hom), np.float64).reshape(-1, 2)
    return out
