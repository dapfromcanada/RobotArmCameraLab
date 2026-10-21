"""Turn a :class:`ChartSpec` into a drawing (shapes in mm), and rasterise it.

The same drawing feeds the PDF writer and :func:`rasterize`, so tests can check
the printed geometry (e.g. that a tag is detected with the right id) without a
printer or a PDF renderer.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import cv2
import numpy as np
import numpy.typing as npt

from camera_lab.two_d.charts.models import (
    TAG_CELLS,
    TAG_FAMILY,
    ChartSpec,
    PatchFeature,
    SlantedSquareFeature,
    StarFeature,
    TagSpec,
)

#: Scale bar for checking the print: 50 mm long, ticks every 10 mm.
SCALE_BAR_MM = 50.0
SCALE_TICK_MM = 10.0
SCALE_BAR_THICK_MM = 1.5
SCALE_TICK_LEN_MM = 4.0
SCALE_TICK_W_MM = 0.5
SCALE_LABEL_W_MM = 30.0
#: Bottom-of-page layout (mm from the top edge, Letter): title block and scale bar.
FOOTER_TOP_FROM_BOTTOM_MM = 40.0
MARGIN_MM = 15.0
LINE_STEP_MM = 6.0
TITLE_PT = 11.0
BODY_PT = 8.0
TAG_LABEL_PT = 8.0
TAG_LABEL_GAP_MM = 5.0
#: Points per Siemens-star wedge arc (smoothness of the outer edge).
STAR_ARC_POINTS = 8
#: rasterize(): supersampling factor (≤ 8) and the finest grid it may use.
RASTER_SUPERSAMPLE = 8
RASTER_FINE_PX_PER_MM = 32.0
PRINT_NOTE = (
    "Print at 100 % (actual size, no 'fit to page') on matte paper. Check: scale bar = 50 mm."
)


@dataclass(frozen=True)
class Rect:
    x: float
    y: float
    w: float
    h: float
    grey: float


@dataclass(frozen=True)
class Poly:
    points: tuple[tuple[float, float], ...]
    grey: float


@dataclass(frozen=True)
class Text:
    x: float  # left of the baseline, mm
    y: float  # baseline, mm from the top edge
    size_pt: float
    text: str


Shape = Rect | Poly | Text


def tag_bits(tag_id: int) -> npt.NDArray[np.uint8]:
    """The tag as an 8 × 8 array of cells (0 = black, 255 = white), border included."""
    if TAG_FAMILY != "36h11":  # pragma: no cover - single family for now
        raise ValueError(TAG_FAMILY)
    dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_APRILTAG_36h11)
    return np.asarray(cv2.aruco.generateImageMarker(dictionary, tag_id, TAG_CELLS), np.uint8)


def _tag_shapes(tag: TagSpec) -> list[Shape]:
    bits = tag_bits(tag.id)
    c = tag.cell_mm
    out: list[Shape] = []
    for row in range(TAG_CELLS):
        for col in range(TAG_CELLS):
            if bits[row, col] == 0:
                out.append(Rect(tag.x_mm + col * c, tag.y_mm + row * c, c, c, 0.0))
    out.append(
        Text(tag.x_mm, tag.y_mm + tag.size_mm + TAG_LABEL_GAP_MM, TAG_LABEL_PT, f"tag {tag.id}")
    )
    return out


def _star_shapes(star: StarFeature) -> list[Shape]:
    out: list[Shape] = []
    half = math.pi / star.spokes  # each black wedge spans half a period
    for i in range(star.spokes):
        a0 = 2 * math.pi * i / star.spokes
        pts = [(star.cx_mm, star.cy_mm)]
        for k in range(STAR_ARC_POINTS + 1):
            a = a0 + half * k / STAR_ARC_POINTS
            pts.append(
                (
                    star.cx_mm + star.radius_mm * math.cos(a),
                    star.cy_mm + star.radius_mm * math.sin(a),
                )
            )
        out.append(Poly(tuple(pts), 0.0))
    return out


def _footer(spec: ChartSpec) -> list[Shape]:
    top = spec.page_mm[1] - FOOTER_TOP_FROM_BOTTOM_MM
    lines = [
        Text(MARGIN_MM, top, TITLE_PT, spec.title),
        Text(
            MARGIN_MM,
            top + LINE_STEP_MM,
            BODY_PT,
            f"chart id {spec.id}   version {spec.version}   type {spec.type}   "
            f"tags {', '.join(str(t.id) for t in spec.tags)} (AprilTag {TAG_FAMILY})",
        ),
        Text(MARGIN_MM, top + 2 * LINE_STEP_MM, BODY_PT, "camera_lab test chart. " + PRINT_NOTE),
    ]
    # own row below the text: label on the left, bar to its right
    bar_x = MARGIN_MM + SCALE_LABEL_W_MM
    bar_y = top + 3 * LINE_STEP_MM + SCALE_TICK_LEN_MM
    shapes: list[Shape] = [*lines, Rect(bar_x, bar_y, SCALE_BAR_MM, SCALE_BAR_THICK_MM, 0.0)]
    ticks = round(SCALE_BAR_MM / SCALE_TICK_MM)
    for i in range(ticks + 1):
        x = bar_x + i * SCALE_TICK_MM - SCALE_TICK_W_MM * (i == ticks)  # last tick ends at 50 mm
        shapes.append(Rect(x, bar_y - SCALE_TICK_LEN_MM, SCALE_TICK_W_MM, SCALE_TICK_LEN_MM, 0.0))
    shapes.append(Text(MARGIN_MM, bar_y + SCALE_BAR_THICK_MM, BODY_PT, "scale bar = 50 mm:"))
    return shapes


def chart_drawing(spec: ChartSpec) -> list[Shape]:
    """All shapes of the chart, in drawing order (features, tags, footer)."""
    shapes: list[Shape] = []
    for f in spec.features:
        if isinstance(f, PatchFeature):
            shapes.append(Rect(f.x_mm, f.y_mm, f.w_mm, f.h_mm, f.grey))
        elif isinstance(f, SlantedSquareFeature):
            shapes.append(Poly(f.corners(), f.grey))
        else:
            shapes.extend(_star_shapes(f))
    for t in spec.tags:
        shapes.extend(_tag_shapes(t))
    shapes.extend(_footer(spec))
    return shapes


def rasterize(spec: ChartSpec, px_per_mm: float) -> npt.NDArray[np.uint8]:
    """Render the chart (text omitted) as an 8-bit grey image, paper = 255.

    Geometry is exact to ~1/16 px: chart point (x, y) mm lands on pixel
    coordinate (x · px_per_mm, y · px_per_mm) in OpenCV's convention (pixel
    centres at integers), as a camera with that scale would see it. Shapes are
    filled without anti-aliasing on a supersampled grid, then area-averaged
    (anti-aliased ``fillPoly`` grows every shape by half a pixel).
    """
    w, h = spec.page_mm
    out_h, out_w = round(h * px_per_mm), round(w * px_per_mm)
    factor = max(1, min(RASTER_SUPERSAMPLE, int(RASTER_FINE_PX_PER_MM // px_per_mm)))
    fine = np.full((out_h * factor, out_w * factor), 255, np.uint8)
    shift = 4  # sub-pixel vertex precision for cv2.fillPoly
    for s in chart_drawing(spec):
        if isinstance(s, Text):
            continue
        if isinstance(s, Rect):
            pts = [(s.x, s.y), (s.x + s.w, s.y), (s.x + s.w, s.y + s.h), (s.x, s.y + s.h)]
        else:
            pts = list(s.points)
        # output centre u ↔ fine centre u·factor + (factor − 1)/2
        fine_pts = np.array(pts) * px_per_mm * factor + (factor - 1) / 2
        poly = np.round(fine_pts * (1 << shift)).astype(np.int32)
        cv2.fillPoly(fine, [poly], (float(round(s.grey * 255)),), lineType=cv2.LINE_8, shift=shift)
    if factor == 1:
        return fine
    return np.asarray(cv2.resize(fine, (out_w, out_h), interpolation=cv2.INTER_AREA), np.uint8)
