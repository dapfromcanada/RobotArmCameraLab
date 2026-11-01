"""Focus meter (Derek 2026-10-26: "a focus tool, that way we can see what is happening").

Live sharpness on the library's resolution chart: MTF50 per colour plane (one plane for mono),
the best value so far (reset by itself when the chart moves), and plain warnings for what
spoiled the bench test of 2026-10-25: a chart too small to measure, light that dropped, clipped
highlights, an exposure at its limit. Front ends (GUI Focus window, setup) show the readings.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from statistics import median

import numpy as np
import numpy.typing as npt

from camera_lab.two_d.charts.detect import (
    ChartDetection,
    chart_to_image,
    charts_from_tags,
    detect_tags,
)
from camera_lab.two_d.charts.field import CORNERS, SIDES, SOFTER_SIDE
from camera_lab.two_d.charts.library import tag_owner
from camera_lab.two_d.charts.models import ChartSpec, SlantedSquareFeature
from camera_lab.two_d.charts.scores import _edge_mtf, square_corners
from camera_lab.two_d.models import Frame
from camera_lab.two_d.processing import demosaic, to_float01

#: Fewer measurable edges than this (of 8): the chart is too small in the image.
MIN_EDGES = 6
#: Longest exposure the focus readings choose: long ones blur if the bench moves, and the
#: JAI's darks go bad above ≈ 100 ms (2026-10-25).
FOCUS_MAX_EXPOSURE_US = 100_000.0
#: The chart moved (centre shift as a fraction of the image width, or scale change): the
#: best value belongs to the old position, so it starts again.
MOVE_SHIFT = 0.03
MOVE_SCALE = 0.05
#: Light per µs below this fraction of the first reading: say so (iris or lights changed).
LIGHT_DROP = 0.5
#: Past the peak: the current value below this fraction of the best.
PAST_PEAK = 0.9
#: Highlights (99.5th percentile) the exposure aims for, and when it re-aims.
TARGET = 0.7
TARGET_BAND = (0.5, 0.85)
CLIP_LEVEL = 0.98


@dataclass(frozen=True)
class FocusReading:
    """One live reading."""

    found: bool  # the resolution chart is in view
    mtf50: dict[str, float | None]  # plane (R, G, B or Y) → MTF50, cycles per pixel
    main: float | None  # G (colour) or Y (mono): the number to focus on
    best: dict[str, float]  # best so far per plane
    edges: int  # edges measured on the main plane
    edges_total: int
    px_per_mm: float | None  # chart scale in the image
    highlights: float  # 99.5th percentile, fraction of full scale
    clipped_pct: float
    exposure_us: float
    warnings: tuple[str, ...] = ()
    reset: bool = False  # the best restarted with this reading (chart moved)
    hint: str = ""  # what to do with the focus ring now
    lp_mm: float | None = None  # main plane, line pairs per mm on the chart
    planes: tuple[str, ...] = field(default=())
    #: field chart in view (Phase 10 step 7): MTF50 per sheet role (main plane), the worst
    #: corner and the softer side (a tilted lens or sensor), focus on the centre sheet
    field_mtf50: dict[str, float | None] = field(default_factory=dict)
    corner_worst: tuple[str, float] | None = None
    softer_side: str | None = None
    #: the chart focused on (its id): a focus baseline compares only on the same chart
    chart: str | None = None

    @property
    def best_main(self) -> float | None:
        key = "G" if "G" in self.best else "Y"
        return self.best.get(key)


def _planes(frame: Frame) -> dict[str, npt.NDArray[np.float64]]:
    img = to_float01(demosaic(frame.data, frame.pixel_format), frame.bit_depth)
    if img.ndim == 3:
        return {"R": img[..., 0], "G": img[..., 1], "B": img[..., 2]}
    return {"Y": img}


class FocusMeter:
    """Readings for one focusing session; keeps the best value and the reference position."""

    def __init__(self, library: dict[int, ChartSpec] | None = None) -> None:
        self._library = library if library is not None else tag_owner()
        self._best: dict[str, float] = {}
        self._ref_pos: tuple[float, float, float] | None = None  # x, y, scale
        self._ref_light: float | None = None  # highlights per µs

    def reset(self) -> None:
        self._best = {}
        self._ref_pos = None
        self._ref_light = None

    def read(self, frame: Frame, exposure_us: float, *, at_limit: bool = False) -> FocusReading:
        full = float((1 << frame.bit_depth) - 1)
        sample = np.asarray(frame.data)[::4, ::4]
        highlights = float(np.percentile(sample, 99.5)) / full
        clipped = float(np.mean(sample >= CLIP_LEVEL * full)) * 100
        warnings: list[str] = []
        if clipped > 0.2:
            warnings.append(f"highlights clipped ({clipped:.1f} %): the exposure is lowered")
        if at_limit and highlights < TARGET_BAND[0]:
            warnings.append(
                "too dark: the exposure is at its limit — more light on the chart (floods aimed?)"
            )
        light = highlights / max(exposure_us, 1e-9)
        if self._ref_light is None:
            self._ref_light = light
        elif light < LIGHT_DROP * self._ref_light:
            warnings.append(
                f"the light is {light / self._ref_light:.2f}× what it was at the start "
                "(iris closed? lights moved or shaded?)"
            )
        spec, det, field_sheets = self._resolution(frame)
        base = dict(
            highlights=round(highlights, 3),
            clipped_pct=round(clipped, 3),
            exposure_us=float(exposure_us),
        )
        if det is None or spec is None:
            return FocusReading(
                found=False,
                mtf50={},
                main=None,
                best=dict(self._best),
                edges=0,
                edges_total=0,
                px_per_mm=None,
                warnings=(
                    *warnings,
                    "no resolution chart in view: the tag at its top left must be visible "
                    "(or the field chart's centre sheet)",
                ),
                hint="put the resolution chart (or the field chart) in view",
                **base,  # type: ignore[arg-type]
            )
        squares = [f for f in spec.features if isinstance(f, SlantedSquareFeature)]
        centre = chart_to_image(det, [[f.cx_mm, f.cy_mm] for f in squares]).mean(axis=0)
        a, b = chart_to_image(det, [[0.0, 0.0], [1.0, 0.0]])
        scale = float(np.linalg.norm(b - a))
        reset = self._moved(float(centre[0]), float(centre[1]), scale, frame.data.shape[1])
        planes = _planes(frame)
        mtf: dict[str, float | None] = {}
        lp: dict[str, float | None] = {}
        counts: dict[str, int] = {}
        clipped_edges = 0
        # the squares as found in the image: the one tag misplaces them near the image
        # edge or close to a wide lens (2026-10-28)
        main_plane = planes["G" if "G" in planes else "Y"]
        found = {f.name: square_corners(main_plane, det, f) for f in squares}
        for name, plane in planes.items():
            edges = [_edge_mtf(plane, det, f, k, found[f.name]) for f in squares for k in range(4)]
            ok = [e for e in edges if e.get("mtf50_cyc_px") is not None]
            if name == ("G" if "G" in planes else "Y"):
                clipped_edges = sum("clipped" in str(e.get("error", "")) for e in edges)
            counts[name] = len(ok)
            mtf[name] = round(float(median(e["mtf50_cyc_px"] for e in ok)), 4) if ok else None
            vals = [e["mtf50_lp_mm"] for e in ok if e.get("mtf50_lp_mm") is not None]
            lp[name] = round(float(median(vals)), 3) if vals else None
        main_key = "G" if "G" in planes else "Y"
        total = 4 * len(squares)
        if clipped_edges:
            warnings.append(
                f"chart white clipped on {clipped_edges} of {total} edges: they are not "
                "measured (a clipped edge reads far too sharp) — lower the exposure"
            )
        elif counts[main_key] < MIN_EDGES:
            warnings.append(
                f"chart too small in the image ({scale:.1f} px/mm): only {counts[main_key]} of "
                f"{total} edges measurable — move it closer (or use a longer lens)"
            )
        for name, v in mtf.items():
            if v is not None and v > self._best.get(name, 0.0):
                self._best[name] = v
        main = mtf[main_key]
        best = self._best.get(main_key)
        field_mtf, worst, softer = self._field(main_plane, field_sheets)
        if main is not None and field_mtf:
            field_mtf["centre"] = main
        if main is None:
            hint = "no sharp edge found: turn the focus ring slowly"
        elif best is not None and main < PAST_PEAK * best:
            hint = "past the sharpest point: turn back towards the best"
        else:
            hint = "turn slowly; stop where the number peaks"
        return FocusReading(
            found=True,
            mtf50=mtf,
            main=main,
            best=dict(self._best),
            edges=counts[main_key],
            edges_total=total,
            px_per_mm=round(scale, 3),
            warnings=tuple(warnings),
            reset=reset,
            hint=hint,
            lp_mm=lp[main_key],
            planes=tuple(planes),
            field_mtf50=field_mtf,
            corner_worst=worst,
            softer_side=softer,
            chart=spec.id,
            **base,
        )

    @staticmethod
    def _field(
        plane: npt.NDArray[np.float64], sheets: list[tuple[ChartSpec, ChartDetection]]
    ) -> tuple[dict[str, float | None], tuple[str, float] | None, str | None]:
        """MTF50 of each field sheet other than the centre, the worst corner, the softer
        side."""
        values: dict[str, float | None] = {}
        for spec, det in sheets:
            if spec.role in (None, "centre"):
                continue
            squares = [f for f in spec.features if isinstance(f, SlantedSquareFeature)]
            edges = [
                _edge_mtf(plane, det, f, k, square_corners(plane, det, f))
                for f in squares
                for k in range(4)
            ]
            ok = [e["mtf50_cyc_px"] for e in edges if e.get("mtf50_cyc_px") is not None]
            values[str(spec.role)] = round(float(median(ok)), 4) if ok else None
        corners = {k: v for k, v in values.items() if k in CORNERS and v is not None}
        worst = min(corners.items(), key=lambda kv: kv[1]) if corners else None
        sides = {}
        for side, roles in SIDES.items():
            vals = [values[r] for r in roles if values.get(r) is not None]
            sides[side] = sum(vals) / len(vals) if vals else None  # type: ignore[arg-type]
        softer = []
        for a, b in (("left", "right"), ("top", "bottom")):
            va, vb = sides[a], sides[b]
            if va and vb and abs(va - vb) / ((va + vb) / 2) > SOFTER_SIDE:
                softer.append(a if va < vb else b)
        return values, worst, ", ".join(softer) or None

    def _resolution(
        self, frame: Frame
    ) -> tuple[ChartSpec | None, ChartDetection | None, list[tuple[ChartSpec, ChartDetection]]]:
        """The chart to focus on (the resolution chart, else the field chart's centre
        sheet) and the field-chart sheets in view."""
        scan = charts_from_tags(detect_tags(frame.data, frame.pixel_format), self._library)
        by_id = {c.id: c for c in self._library.values()}
        found = [(by_id[d.chart_id], d) for d in scan.charts if d.chart_id in by_id]
        field_sheets = [(s, d) for s, d in found if s.type == "field"]
        for s, d in found:
            if s.type == "resolution":
                return s, d, field_sheets
        for s, d in field_sheets:
            if s.role == "centre":
                return s, d, field_sheets
        return None, None, field_sheets

    def _moved(self, x: float, y: float, scale: float, width: int) -> bool:
        """True when the chart moved since the reference (the best then starts again)."""
        ref = self._ref_pos
        self._ref_pos = (x, y, scale) if ref is None else ref
        if ref is None:
            return False
        shift = math.hypot(x - ref[0], y - ref[1]) / max(width, 1)
        if shift > MOVE_SHIFT or abs(scale / ref[2] - 1) > MOVE_SCALE:
            self._best = {}
            self._ref_pos = (x, y, scale)
            self._ref_light = None
            return True
        return False


def next_exposure(reading: FocusReading, lo: float, hi: float) -> float | None:
    """A new exposure when the highlights left the band (None: keep it)."""
    h = reading.highlights
    if reading.clipped_pct > 0.2 or h > TARGET_BAND[1]:
        return max(lo, reading.exposure_us * (0.5 if h >= CLIP_LEVEL else TARGET / h))
    if h < TARGET_BAND[0]:
        return min(hi, reading.exposure_us * TARGET / max(h, 0.02))
    return None
