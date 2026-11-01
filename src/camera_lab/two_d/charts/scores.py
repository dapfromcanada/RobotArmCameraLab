"""Chart scores (Phase 10 step 3): measurements against known targets.

Every measurement region comes from the chart layout mapped through the
detection's homography; nobody draws boxes. Images are used linearly (codes /
full scale) after demosaicing; grey-level metrics use Rec. 709 luma.

- **step wedge:** level and spatial noise of each step; how many steps can be
  told apart (adjacent steps differ by more than ``SEPARATION_SIGMA`` × noise
  and neither is clipped); clipped steps; monotonicity; R² of level against the
  *nominal* print grey (office prints are not calibrated: compare cameras on
  the same print, not against absolute values).
- **grey patch:** level, noise, SNR, colour-channel spread (white-balance
  error, colour cameras only) and contrast against the black reference.
- **resolution:** slanted-edge MTF (ISO 12233 style, simplified) on the four
  edges of each slanted square: edge line fitted per row, 4× oversampled edge
  spread function, Hamming-windowed line spread function, FFT, corrected for
  the finite-difference derivative. MTF50 in cycles/pixel and in line pairs per
  mm at the chart (using the local scale from the homography). Vertical edges
  measure horizontal resolution and vice versa.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Mapping
from statistics import median
from typing import Any

import cv2
import numpy as np
import numpy.typing as npt

from camera_lab.two_d.charts.detect import (
    ChartDetection,
    chart_to_image,
    charts_from_tags,
    detect_tags,
)
from camera_lab.two_d.charts.models import (
    ChartSpec,
    PatchFeature,
    SlantedSquareFeature,
)
from camera_lab.two_d.models import Frame
from camera_lab.two_d.processing import demosaic, to_float01, to_luma

#: Patch sampling: inset from each side (fraction) and the minimum pixel count.
PATCH_INSET = 0.2
MIN_PATCH_PX = 25
#: A level at or above this (linear 0–1) counts as clipped.
CLIP_LEVEL = 0.98
#: Slanted edge: more than this share of its pixels at CLIP_LEVEL and the edge is not measured.
#: A clipped white flattens the edge profile and reads far too sharp (2026-11-01: the mono
#: read 0.43 cycles/px instead of 0.25 with the chart white partly saturated).
EDGE_MAX_CLIPPED_FRAC = 0.002
#: Adjacent wedge steps are told apart when they differ by more than this × noise.
SEPARATION_SIGMA = 2.0
#: Slanted edge: ROI length along the edge (fraction of the side) and half width (mm).
EDGE_ROI_LEN_FRAC = 0.6
EDGE_ROI_HALF_WIDTH_MM = 6.0
#: ...but never narrower than this across (px each side): a small chart in the image
#: (~2 px/mm) with a soft lens needs room for the blur on both sides.
EDGE_ROI_MIN_HALF_PX = 16.0
EDGE_MIN_ROWS = 20
EDGE_MIN_HALF_WIDTH_PX = 8
EDGE_MIN_ANGLE_DEG = 1.0
EDGE_MIN_CONTRAST = 0.05
#: Locating the edge: rows whose strongest step is further than this from the rough line
#: are left out; the precise centroid uses a band of max(min, region width / div) px.
EDGE_ROUGH_TOL_PX = 3.0
#: Finding a slanted square in the image: search margin and largest centre shift (× its
#: predicted side), and the accepted area (× predicted side²).
SQUARE_SEARCH_PAD = 0.4
SQUARE_MAX_SHIFT = 0.3
SQUARE_AREA_RANGE = (0.6, 1.6)
EDGE_BAND_MIN_PX = 10
EDGE_BAND_DIV = 8
OVERSAMPLE = 4
#: Edge names of a slanted square, in corner order (top, right, bottom, left before rotation).
EDGE_SIDES = ("top", "right", "bottom", "left")


def _linear_image(
    data: npt.NDArray[Any], pixel_format: str, bit_depth: int
) -> npt.NDArray[np.float64]:
    return to_float01(demosaic(data, pixel_format), bit_depth)


def _r(v: float | None, nd: int = 5) -> float | None:
    return None if v is None or not math.isfinite(v) else round(float(v), nd)


def _pixel_luma(pix: npt.NDArray[np.float64]) -> npt.NDArray[np.float64]:
    """Luma of sampled pixels: (N,) mono stays, (N, 3) RGB → Rec. 709."""
    return pix if pix.ndim == 1 else to_luma(pix)


# -------------------------------------------------------------- sampling
def _sample_quad(
    img: npt.NDArray[np.float64], quad_px: npt.NDArray[np.float64]
) -> npt.NDArray[np.float64] | None:
    """Pixels inside a convex quadrilateral (image px); (N,) or (N, 3); None if too few."""
    h, w = img.shape[:2]
    x0, y0 = np.floor(quad_px.min(axis=0)).astype(int)
    x1, y1 = np.ceil(quad_px.max(axis=0)).astype(int) + 1
    x0, y0, x1, y1 = max(x0, 0), max(y0, 0), min(x1, w), min(y1, h)
    if x1 - x0 < 2 or y1 - y0 < 2:
        return None
    mask = np.zeros((y1 - y0, x1 - x0), np.uint8)
    shift = 4
    pts = np.round((quad_px - [x0, y0]) * (1 << shift)).astype(np.int32)
    cv2.fillConvexPoly(mask, pts, (1.0,), lineType=cv2.LINE_8, shift=shift)
    pix = img[y0:y1, x0:x1][mask > 0]
    return pix if len(pix) >= MIN_PATCH_PX else None


def _patch_pixels(
    img: npt.NDArray[np.float64], det: ChartDetection, f: PatchFeature
) -> npt.NDArray[np.float64] | None:
    ix, iy = f.w_mm * PATCH_INSET, f.h_mm * PATCH_INSET
    quad = [
        (f.x_mm + ix, f.y_mm + iy),
        (f.x_mm + f.w_mm - ix, f.y_mm + iy),
        (f.x_mm + f.w_mm - ix, f.y_mm + f.h_mm - iy),
        (f.x_mm + ix, f.y_mm + f.h_mm - iy),
    ]
    return _sample_quad(img, chart_to_image(det, quad))


# ------------------------------------------------------------ step wedge
def score_step_wedge(
    img: npt.NDArray[np.float64], det: ChartDetection, spec: ChartSpec
) -> dict[str, Any]:
    luma = to_luma(img)
    steps = sorted((f for f in spec.features if isinstance(f, PatchFeature)), key=lambda f: f.grey)
    levels: list[float | None] = []
    noise: list[float | None] = []
    for f in steps:
        pix = _patch_pixels(luma, det, f)
        levels.append(None if pix is None else float(pix.mean()))
        noise.append(None if pix is None else float(pix.std()))
    seen = [i for i, v in enumerate(levels) if v is not None]
    if len(seen) < 2:
        return {"error": "fewer than 2 wedge steps are inside the image"}
    lv = {i: float(levels[i] or 0.0) for i in seen}
    nz = {i: float(noise[i] or 0.0) for i in seen}
    clipped = [i in lv and lv[i] >= CLIP_LEVEL for i in range(len(steps))]
    separated = 0
    monotonic = True
    for a, b in itertools.pairwise(seen):
        sigma = math.sqrt((nz[a] ** 2 + nz[b] ** 2) / 2)
        if (
            not clipped[a]
            and not clipped[b]
            and lv[b] - lv[a] > SEPARATION_SIGMA * max(sigma, 1e-6)
        ):
            separated += 1
        if lv[b] < lv[a] - max(nz[a], nz[b]):
            monotonic = False
    fit = [(steps[i].grey, lv[i]) for i in seen if not clipped[i]]
    r2 = None
    if len(fit) >= 3:
        x, y = np.array(fit, np.float64).T
        coef = np.polyfit(x, y, 1)
        resid = y - np.polyval(coef, x)
        tot = float(((y - y.mean()) ** 2).sum())
        r2 = 1.0 - float((resid**2).sum()) / tot if tot > 0 else None
    return {
        "steps_seen": len(seen),
        "steps_resolved": separated + 1,
        "clipped_steps": int(sum(clipped)),
        "monotonic": monotonic,
        "r2_vs_nominal_grey": _r(r2),
        "levels": [_r(v) for v in levels],
        "noise": [_r(v) for v in noise],
    }


# ------------------------------------------------------------ grey patch
def score_grey_patch(
    img: npt.NDArray[np.float64], det: ChartDetection, spec: ChartSpec
) -> dict[str, Any]:
    feats = {f.name: f for f in spec.features if isinstance(f, PatchFeature)}
    grey = _patch_pixels(img, det, feats["grey"]) if "grey" in feats else None
    if grey is None:
        return {"error": "grey patch is not inside the image"}
    luma = _pixel_luma(grey)
    level, sd = float(luma.mean()), float(luma.std())
    out: dict[str, Any] = {
        "level": _r(level),
        "noise": _r(sd),
        "snr": _r(level / sd if sd > 0 else None, 2),
        "clipped": bool(level >= CLIP_LEVEL),
        "wb_spread_pct": None,
        "channel_means": None,
        "contrast_vs_black": None,
    }
    if grey.ndim == 2:
        means = grey.mean(axis=0)
        out["channel_means"] = {
            c: _r(float(v)) for c, v in zip(("red", "green", "blue"), means, strict=True)
        }
        if means.mean() > 0:
            out["wb_spread_pct"] = _r(float((means.max() - means.min()) / means.mean() * 100), 3)
    black = _patch_pixels(img, det, feats["black-ref"]) if "black-ref" in feats else None
    if black is not None:
        bl = float(_pixel_luma(black).mean())
        out["black_level"] = _r(bl)
        out["contrast_vs_black"] = _r(level / bl if bl > 0 else None, 2)
    return out


# ------------------------------------------------------------- resolution
def _locate_edge(sub: npt.NDArray[np.float64]) -> npt.NDArray[np.float64] | str:
    """The edge in the region as a curve, column = poly(row), or why there is none.

    Rough first: the strongest step in each row (smoothed along the edge), a robust line.
    Then precise: the gradient centroid in a narrow band around that line, with the band's
    background gradient removed, so noise and paper texture across a wide region (a chart
    close to the lens) cannot pull the estimate off the edge. Degree 2 (ISO 12233:2017
    allows a polynomial edge) follows the slight bend lens distortion gives a straight edge
    away from the image centre."""
    rows, cols = sub.shape
    grad = np.abs(np.diff(sub, axis=1))
    r = np.arange(rows, dtype=np.float64)
    rough = cv2.blur(grad, (3, 9)).argmax(axis=1).astype(np.float64) + 0.5
    keep = np.ones(rows, bool)
    for _ in range(3):
        coef = np.polyfit(r[keep], rough[keep], 1)
        keep = np.abs(rough - np.polyval(coef, r)) < EDGE_ROUGH_TOL_PX
        if keep.sum() < EDGE_MIN_ROWS:
            return "no straight edge found in the region"
    band = max(EDGE_BAND_MIN_PX, cols // EDGE_BAND_DIV)
    at = np.arange(cols - 1, dtype=np.float64) + 0.5
    inside = np.abs(at[None, :] - np.polyval(coef, r)[:, None]) <= band
    in_band = np.where(inside, grad, np.nan)
    background = np.nanmedian(in_band, axis=1)
    g = np.clip(np.nan_to_num(in_band) - background[:, None], 0.0, None)
    weight = g.sum(axis=1)
    ok = keep & (weight > 0)
    if ok.sum() < EDGE_MIN_ROWS:
        return "no edge found in most rows"
    cen = (g[ok] * at).sum(axis=1) / weight[ok]
    deg = 2 if ok.sum() >= 3 * EDGE_MIN_ROWS else 1
    fit = np.polyfit(r[ok], cen, deg)
    good = np.abs(cen - np.polyval(fit, r[ok])) < 2.0
    if good.sum() >= EDGE_MIN_ROWS:
        fit = np.polyfit(r[ok][good], cen[good], deg)
    return np.asarray(fit, np.float64)


def square_corners(
    luma: npt.NDArray[np.float64], det: ChartDetection, sq: SlantedSquareFeature
) -> npt.NDArray[np.float64] | None:
    """The square's four corners as found in the image (in the order of ``sq.corners()``),
    or None when it cannot be found cleanly.

    The homography comes from the chart's one tag. Far from that tag, and where the lens
    bends straight lines (a chart close to a wide lens, or near the image edge), it can miss
    a square by tens of pixels (TRI089S-M + 8 mm, 2026-10-28): the edge regions are then
    placed from the square itself, and the tag only says where to look."""
    pred = chart_to_image(det, sq.corners())
    side = float(np.mean(np.linalg.norm(pred - np.roll(pred, 1, axis=0), axis=1)))
    h, w = luma.shape
    x0, y0 = np.floor(pred.min(axis=0) - SQUARE_SEARCH_PAD * side).astype(int)
    x1, y1 = np.ceil(pred.max(axis=0) + SQUARE_SEARCH_PAD * side).astype(int)
    x0, y0, x1, y1 = max(x0, 0), max(y0, 0), min(x1, w), min(y1, h)
    if x1 - x0 < 2 * EDGE_MIN_HALF_WIDTH_PX or y1 - y0 < 2 * EDGE_MIN_HALF_WIDTH_PX:
        return None
    crop = luma[y0:y1, x0:x1]
    lo, hi = np.percentile(crop, [2, 98])
    if hi - lo < EDGE_MIN_CONTRAST:
        return None
    mask = (crop < (lo + hi) / 2).astype(np.uint8)
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    centre = pred.mean(axis=0) - (x0, y0)
    for c in sorted(contours, key=cv2.contourArea, reverse=True):
        if not SQUARE_AREA_RANGE[0] < cv2.contourArea(c) / side**2 < SQUARE_AREA_RANGE[1]:
            continue
        quad = cv2.approxPolyDP(c, 0.03 * cv2.arcLength(c, True), True)
        if len(quad) != 4 or not cv2.isContourConvex(quad):
            continue
        q = quad.reshape(4, 2).astype(np.float64)
        if np.linalg.norm(q.mean(axis=0) - centre) > SQUARE_MAX_SHIFT * side:
            continue
        found = np.array([q[np.argmin(np.linalg.norm(q - (p - (x0, y0)), axis=1))] for p in pred])
        if len({tuple(p) for p in found}) == 4:
            return np.asarray(found, np.float64) + np.array([x0, y0], np.float64)
    return None


def _edge_mtf(
    luma: npt.NDArray[np.float64],
    det: ChartDetection,
    sq: SlantedSquareFeature,
    k: int,
    corners_px: npt.NDArray[np.float64] | None = None,
) -> dict[str, Any]:
    """MTF of one edge. ``corners_px``: the square as found in the image
    (:func:`square_corners`); without it the region comes from the homography."""
    name = f"{sq.name}/{EDGE_SIDES[k]}"
    corners = np.array(sq.corners())
    p0, p1 = corners[k], corners[(k + 1) % 4]
    mid = (p0 + p1) / 2
    d = (p1 - p0) / np.linalg.norm(p1 - p0)
    n = np.array([-d[1], d[0]])
    pm, pd, pn = chart_to_image(det, [mid, mid + d, mid + n])
    scale = float(np.linalg.norm(pn - pm))  # px per mm across the edge
    if corners_px is not None:
        p0, p1 = corners_px[k], corners_px[(k + 1) % 4]
        mid = (p0 + p1) / 2
        side = float(np.linalg.norm(p1 - p0))
        d = (p1 - p0) / side
        n = np.array([-d[1], d[0]])
        hl = EDGE_ROI_LEN_FRAC / 2 * side
        hw = max(EDGE_ROI_HALF_WIDTH_MM / sq.size_mm * side, EDGE_ROI_MIN_HALF_PX)
        pm, pd = mid, mid + d
    else:
        hl = EDGE_ROI_LEN_FRAC / 2 * sq.size_mm
        hw = max(EDGE_ROI_HALF_WIDTH_MM, EDGE_ROI_MIN_HALF_PX / max(scale, 1e-9))
    quad = [
        mid - d * hl - n * hw,
        mid + d * hl - n * hw,
        mid + d * hl + n * hw,
        mid - d * hl + n * hw,
    ]
    roi = np.array(quad) if corners_px is not None else chart_to_image(det, quad)
    h, w = luma.shape
    x0, y0 = np.ceil(roi.min(axis=0)).astype(int)
    x1, y1 = np.floor(roi.max(axis=0)).astype(int)
    if x0 < 0 or y0 < 0 or x1 > w or y1 > h:
        return {"edge": name, "error": "edge region is not fully inside the image"}
    sub = luma[y0:y1, x0:x1]
    horizontal = abs((pd - pm)[0]) > abs((pd - pm)[1])  # edge runs along x in the image
    if horizontal:
        sub = sub.T
    rows, cols = sub.shape
    if rows < EDGE_MIN_ROWS or cols < 2 * EDGE_MIN_HALF_WIDTH_PX:
        return {"edge": name, "error": f"edge region too small ({rows} x {cols} px)"}
    edge = _locate_edge(sub)
    if isinstance(edge, str):
        return {"edge": name, "error": edge}
    r = np.arange(rows, dtype=np.float64)
    a = float(np.polyval(np.polyder(edge), rows / 2))  # slope at the middle row
    angle = math.degrees(math.atan(a))
    if abs(angle) < EDGE_MIN_ANGLE_DEG:
        return {
            "edge": name,
            "error": f"edge is {angle:.2f}° from the pixel axis (needs ≥ {EDGE_MIN_ANGLE_DEG}°)",
        }
    line = np.polyval(edge, r)
    cos_r = np.cos(np.arctan(np.polyval(np.polyder(edge), r)))
    cc = np.arange(cols, dtype=np.float64)[None, :]
    dist = (cc - line[:, None]) * cos_r[:, None]
    half = float(np.min(np.minimum(line, cols - 1 - line) * cos_r)) - 1.0
    if half < EDGE_MIN_HALF_WIDTH_PX:
        return {"edge": name, "error": "edge too close to the region border"}
    sel = np.abs(dist) <= half
    clipped_frac = float(np.mean(sub[sel] >= CLIP_LEVEL))
    if clipped_frac > EDGE_MAX_CLIPPED_FRAC:
        return {
            "edge": name,
            "error": f"edge clipped ({clipped_frac:.1%} of its pixels at full scale): "
            "lower the exposure",
        }
    nbins = int(2 * half * OVERSAMPLE)
    idx = np.floor((dist[sel] + half) * OVERSAMPLE).astype(int).clip(0, nbins - 1)
    counts = np.bincount(idx, minlength=nbins)
    sums = np.bincount(idx, weights=sub[sel], minlength=nbins)
    filled = counts > 0
    if filled.sum() < nbins // 2:
        return {"edge": name, "error": "too few samples across the edge"}
    pos = np.arange(nbins)
    esf = np.interp(pos, pos[filled], sums[filled] / counts[filled])
    lo_side, hi_side = esf[: max(1, nbins // 8)].mean(), esf[-max(1, nbins // 8) :].mean()
    dark, light = float(min(lo_side, hi_side)), float(max(lo_side, hi_side))
    if light - dark < EDGE_MIN_CONTRAST:
        return {"edge": name, "error": f"edge contrast too low ({light - dark:.3f})"}
    lsf = np.gradient(esf)
    lsf = np.roll(lsf, nbins // 2 - int(np.argmax(np.abs(lsf))))
    lsf = lsf * np.hamming(nbins)
    mtf = np.abs(np.fft.rfft(lsf))
    if mtf[0] <= 0:
        return {"edge": name, "error": "flat edge profile"}
    freqs = np.fft.rfftfreq(nbins, d=1.0 / OVERSAMPLE)  # cycles per pixel
    upto = freqs <= 1.0
    mtf, freqs = mtf[upto] / mtf[0], freqs[upto]
    mtf = mtf / np.maximum(np.sinc(2 * freqs / OVERSAMPLE), 0.1)  # central-difference correction
    below = np.nonzero(mtf < 0.5)[0]
    mtf50 = None
    if len(below):
        i = int(below[0])
        f0, f1, m0, m1 = freqs[i - 1], freqs[i], mtf[i - 1], mtf[i]
        mtf50 = float(f0 + (m0 - 0.5) * (f1 - f0) / (m0 - m1))
    return {
        "edge": name,
        "orientation": "horizontal" if horizontal else "vertical",
        "angle_deg": _r(angle, 3),
        "mtf50_cyc_px": _r(mtf50, 4),
        "mtf50_lp_mm": _r(mtf50 * scale if mtf50 is not None else None, 3),
        "mtf_nyquist": _r(float(np.interp(0.5, freqs, mtf)), 4),
        "contrast": _r(light - dark, 4),
        "clipped": bool(light >= CLIP_LEVEL),
        **_edge_geometry(edge, rows, x0, y0, horizontal),
    }


def _edge_geometry(
    edge: npt.NDArray[np.float64], rows: int, x0: int, y0: int, horizontal: bool
) -> dict[str, Any]:
    """Where the edge is in the image: its point at the middle row (sub-pixel, image px), its
    direction (unit vector, image px) and its bow (px over the measured length; lens
    distortion bends straight edges away from the image centre)."""
    rm = (rows - 1) / 2
    col = float(np.polyval(edge, rm))  # the gradient centroid: between pixel centres
    slope = float(np.polyval(np.polyder(edge), rm))
    norm = math.hypot(1.0, slope)
    if horizontal:  # the region was transposed: rows run along x
        mid, direction = (x0 + rm, y0 + col), (1.0 / norm, slope / norm)
    else:
        mid, direction = (x0 + col, y0 + rm), (slope / norm, 1.0 / norm)
    bow = float(edge[0]) * (rows / 2) ** 2 if len(edge) == 3 else 0.0
    return {
        "mid_px": [_r(mid[0], 3), _r(mid[1], 3)],
        "dir": [_r(direction[0], 5), _r(direction[1], 5)],
        "bow_px": _r(bow, 3),
    }


def _med(values: list[float]) -> float | None:
    return median(values) if values else None


def score_resolution(
    img: npt.NDArray[np.float64], det: ChartDetection, spec: ChartSpec
) -> dict[str, Any]:
    luma = to_luma(img)
    squares = [f for f in spec.features if isinstance(f, SlantedSquareFeature)]
    found = {f.name: square_corners(luma, det, f) for f in squares}
    edges = [_edge_mtf(luma, det, f, k, found[f.name]) for f in squares for k in range(4)]
    for e in edges:
        e["placed_by"] = "square" if found[e["edge"].split("/")[0]] is not None else "tag"
    ok = [e for e in edges if e.get("mtf50_cyc_px") is not None]
    # vertical edges (varying along x) measure horizontal resolution
    hor = [e["mtf50_cyc_px"] for e in ok if e["orientation"] == "vertical"]
    ver = [e["mtf50_cyc_px"] for e in ok if e["orientation"] == "horizontal"]
    return {
        "edges_measured": len(ok),
        "mtf50_cyc_px": _r(_med([e["mtf50_cyc_px"] for e in ok]), 4),
        "mtf50_lp_mm": _r(_med([e["mtf50_lp_mm"] for e in ok if e["mtf50_lp_mm"] is not None]), 3),
        "mtf50_horizontal_cyc_px": _r(_med(hor), 4),
        "mtf50_vertical_cyc_px": _r(_med(ver), 4),
        "any_clipped": any(e.get("clipped") for e in ok),
        "edges": edges,
    }


SCORERS = {
    "step_wedge": score_step_wedge,
    "grey_patch": score_grey_patch,
    "resolution": score_resolution,
}


def score_chart(
    data: npt.NDArray[Any], pixel_format: str, bit_depth: int, det: ChartDetection, spec: ChartSpec
) -> dict[str, Any] | None:
    """Scores of one detected chart (None for chart types without scores, e.g. tag cards)."""
    if spec.type == "field":  # lazy: the field module builds on this one
        from camera_lab.two_d.charts.field import score_field_sheet

        return score_field_sheet(_linear_image(data, pixel_format, bit_depth), det, spec)
    scorer = SCORERS.get(spec.type)
    if scorer is None:
        return None
    return scorer(_linear_image(data, pixel_format, bit_depth), det, spec)


def chart_records_array(
    data: npt.NDArray[Any], pixel_format: str, bit_depth: int, library: Mapping[int, ChartSpec]
) -> list[dict[str, Any]]:
    """Detect the library charts in an image and score them: sidecar-ready records."""
    scan = charts_from_tags(detect_tags(data, pixel_format), library)
    by_id = {c.id: c for c in library.values()}
    records = scan.to_record()
    for rec, det in zip(records, scan.charts, strict=False):
        scores = score_chart(data, pixel_format, bit_depth, det, by_id[det.chart_id])
        if scores is not None:
            rec["scores"] = scores
    from camera_lab.two_d.charts.field import field_record  # lazy: builds on this module

    fmap = field_record(records)
    return [*records, fmap] if fmap is not None else records


def chart_records(frame: Frame, library: Mapping[int, ChartSpec]) -> list[dict[str, Any]]:
    """:func:`chart_records_array` for a grabbed frame."""
    return chart_records_array(frame.data, frame.pixel_format, frame.bit_depth, library)
