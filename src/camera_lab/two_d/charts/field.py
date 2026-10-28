"""The field chart (Phase 10 step 7, Derek 2026-10-28): nine sheets across the camera view,
centre, edges and corners measured in one image at one distance and one focus.

Per sheet (:func:`score_field_sheet`): where it landed in the image (``r`` = 0 at the image
centre, 1 at a corner); MTF50 of its 8 edges, split into **sagittal** (edge running along
the radius) and **tangential** (edge running across it), horizontal / vertical at the
centre; the paper white's level (and R/G, B/G on colour cameras); the edges' bow; colour
fringing (lateral chromatic aberration: the red and blue edge against the green one, px
outward along the radius, from tangential edges).

Whole image (:func:`field_map`): the sheets side by side and a summary: centre, corners,
corner spread (a tilted lens or sensor makes one side softer), astigmatism, light and
colour at the corners. Light and colour are relative to the centre sheet, so the print's
own grey and colour do not matter.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from statistics import mean, median
from typing import Any

import numpy as np
import numpy.typing as npt

from camera_lab.two_d.charts.detect import ChartDetection
from camera_lab.two_d.charts.library import FIELD_ROLES, FIELD_SET_ID
from camera_lab.two_d.charts.models import ChartSpec, PatchFeature, SlantedSquareFeature
from camera_lab.two_d.charts.scores import (
    CLIP_LEVEL,
    _edge_mtf,
    _patch_pixels,
    _r,
    square_corners,
)
from camera_lab.two_d.processing import to_luma

#: Edges within this field radius are "centre": reported horizontal / vertical.
CENTRE_R = 0.15
#: An edge within this angle of the radius is sagittal, within it of perpendicular tangential;
#: the narrow band between (around 45°) is not classified. Corner sheets sit on the image
#: diagonal: 28° from horizontal on a 1.9 : 1 sensor, 37° on 4 : 3.
CLASS_DEG = 40.0
CORNERS = ("top-left", "top-right", "bottom-right", "bottom-left")
SIDES = {
    "left": ("top-left", "left", "bottom-left"),
    "right": ("top-right", "right", "bottom-right"),
    "top": ("top-left", "top", "top-right"),
    "bottom": ("bottom-left", "bottom", "bottom-right"),
}
EDGES_PER_SHEET = 8
#: One side is "softer" when its sheets read this much below the opposite side (fraction).
SOFTER_SIDE = 0.10


def _med(values: Sequence[float | None]) -> float | None:
    vals = [v for v in values if v is not None]
    return median(vals) if vals else None


def _classify(edge: dict[str, Any], centre: npt.NDArray[np.float64], half_diag: float) -> str:
    """sagittal / tangential / mixed, or horizontal / vertical near the image centre."""
    mid = np.array(edge["mid_px"], np.float64)
    radius = mid - centre
    if np.linalg.norm(radius) / half_diag < CENTRE_R:
        # vertical edges measure horizontal resolution (as in score_resolution)
        return "horizontal" if edge["orientation"] == "vertical" else "vertical"
    u = radius / np.linalg.norm(radius)
    along = math.degrees(math.acos(min(1.0, abs(float(np.dot(edge["dir"], u))))))
    if along <= CLASS_DEG:
        return "sagittal"
    if along >= 90.0 - CLASS_DEG:
        return "tangential"
    return "mixed"


def _fringe(
    plane: npt.NDArray[np.float64],
    det: ChartDetection,
    sq: SlantedSquareFeature,
    k: int,
    corners: npt.NDArray[np.float64] | None,
    green: dict[str, Any],
    centre: npt.NDArray[np.float64],
) -> float | None:
    """This plane's edge against the green one, px outward along the radius (tangential
    edges only: an edge shows a shift across itself, and lateral colour error is radial)."""
    other = _edge_mtf(plane, det, sq, k, corners)
    if "mid_px" not in other:
        return None
    d = np.array(green["dir"], np.float64)
    normal = np.array([-d[1], d[0]])
    shift = float(np.dot(np.array(other["mid_px"]) - np.array(green["mid_px"]), normal))
    radius = np.array(green["mid_px"], np.float64) - centre
    outward = float(np.dot(normal, radius / max(float(np.linalg.norm(radius)), 1e-9)))
    return shift * math.copysign(1.0, outward)


def score_field_sheet(
    img: npt.NDArray[np.float64], det: ChartDetection, spec: ChartSpec
) -> dict[str, Any]:
    """One field-chart sheet in the image (``img`` linear 0–1, mono or RGB)."""
    h, w = img.shape[:2]
    centre = np.array([(w - 1) / 2, (h - 1) / 2])
    half_diag = math.hypot(w, h) / 2
    colour = img.ndim == 3
    luma = to_luma(img) if colour else img
    squares = [f for f in spec.features if isinstance(f, SlantedSquareFeature)]
    found = {f.name: square_corners(luma, det, f) for f in squares}
    edges: list[dict[str, Any]] = []
    fringe_r: list[float] = []
    fringe_b: list[float] = []
    for f in squares:
        for k in range(4):
            e = _edge_mtf(luma, det, f, k, found[f.name])
            e["placed_by"] = "square" if found[f.name] is not None else "tag"
            if e.get("mtf50_cyc_px") is not None:
                e["class"] = _classify(e, centre, half_diag)
                if colour and e["class"] == "tangential":
                    g = _edge_mtf(img[..., 1], det, f, k, found[f.name])
                    if "mid_px" in g:
                        for plane, acc in ((0, fringe_r), (2, fringe_b)):
                            v = _fringe(img[..., plane], det, f, k, found[f.name], g, centre)
                            if v is not None:
                                acc.append(v)
            edges.append(e)
    ok = [e for e in edges if e.get("mtf50_cyc_px") is not None]
    where = [np.mean(c, axis=0) for c in found.values() if c is not None]
    pos = np.mean(where, axis=0) if where else None
    out: dict[str, Any] = {
        "role": spec.role,
        "edges_measured": len(ok),
        "mtf50_cyc_px": _r(_med([e["mtf50_cyc_px"] for e in ok]), 4),
        "mtf50_lp_mm": _r(_med([e.get("mtf50_lp_mm") for e in ok]), 3),
        "any_clipped": any(e.get("clipped") for e in ok),
        "bow_px_max": _r(max((abs(e["bow_px"]) for e in ok), default=None), 3),
        "x": None if pos is None else _r(float(pos[0]) / w, 4),
        "y": None if pos is None else _r(float(pos[1]) / h, 4),
        "r": None if pos is None else _r(float(np.linalg.norm(pos - centre)) / half_diag, 4),
    }
    for cls in ("sagittal", "tangential", "horizontal", "vertical"):
        out[f"mtf50_{cls}_cyc_px"] = _r(
            _med([e["mtf50_cyc_px"] for e in ok if e.get("class") == cls]), 4
        )
    white = next((f for f in spec.features if isinstance(f, PatchFeature) and f.grey == 1.0), None)
    pix = _patch_pixels(img, det, white) if white is not None else None
    if pix is not None:
        level = pix if pix.ndim == 1 else to_luma(pix)
        out["white_level"] = _r(float(level.mean()))
        out["white_clipped"] = bool(float(np.percentile(pix, 99)) >= CLIP_LEVEL)
        if pix.ndim == 2:
            r_, g_, b_ = (float(v) for v in pix.mean(axis=0))
            out["white_rg"] = _r(r_ / g_ if g_ > 0 else None)
            out["white_bg"] = _r(b_ / g_ if g_ > 0 else None)
    if colour:
        out["fringe_red_px"] = _r(max(fringe_r, key=abs) if fringe_r else None, 3)
        out["fringe_blue_px"] = _r(max(fringe_b, key=abs) if fringe_b else None, 3)
    out["edges"] = edges
    return out


def _rel(value: float | None, ref: float | None) -> float | None:
    return None if value is None or not ref else value / ref


def field_map(records: Sequence[dict[str, Any]]) -> dict[str, Any] | None:
    """The whole-image map from the scored sheet records (None when no sheet was seen)."""
    sheets = {
        r["scores"]["role"]: r["scores"]
        for r in records
        if r.get("type") == "field" and isinstance(r.get("scores"), dict)
    }
    if not sheets:
        return None
    c = sheets.get("centre", {})
    c_mtf, c_white = c.get("mtf50_cyc_px"), c.get("white_level")
    rows = []
    for role in FIELD_ROLES:
        s = sheets.get(role)
        if s is None:
            continue
        row = {
            k: s.get(k)
            for k in (
                "role",
                "x",
                "y",
                "r",
                "edges_measured",
                "mtf50_cyc_px",
                "mtf50_sagittal_cyc_px",
                "mtf50_tangential_cyc_px",
                "mtf50_horizontal_cyc_px",
                "mtf50_vertical_cyc_px",
                "bow_px_max",
                "any_clipped",
                "fringe_red_px",
                "fringe_blue_px",
            )
        }
        row["light_vs_centre"] = _r(_rel(s.get("white_level"), c_white), 3)
        for ch in ("rg", "bg"):
            rel = _rel(s.get(f"white_{ch}"), c.get(f"white_{ch}"))
            row[f"{ch}_vs_centre_pct"] = _r(None if rel is None else (rel - 1) * 100, 2)
        rows.append(row)
    by_role = {r["role"]: r for r in rows}
    corners = [by_role[k] for k in CORNERS if by_role.get(k, {}).get("mtf50_cyc_px")]
    corner_mtf = [r["mtf50_cyc_px"] for r in corners]
    worst = min(corners, key=lambda r: r["mtf50_cyc_px"]) if corners else None
    astig = []
    for r in corners:
        sag, tan = r.get("mtf50_sagittal_cyc_px"), r.get("mtf50_tangential_cyc_px")
        if sag and tan:
            astig.append(abs(sag - tan) / ((sag + tan) / 2))
    sides = {}
    for side, roles in SIDES.items():
        vals = [by_role[k]["mtf50_cyc_px"] for k in roles if by_role.get(k, {}).get("mtf50_cyc_px")]
        sides[side] = _r(mean(vals), 4) if vals else None
    softer = None
    for a, b in (("left", "right"), ("top", "bottom")):
        va, vb = sides[a], sides[b]
        if va and vb and abs(va - vb) / ((va + vb) / 2) > SOFTER_SIDE:
            softer = (softer + ", " if softer else "") + (a if va < vb else b)
    fringes = [
        abs(v)
        for r in corners
        for v in (r.get("fringe_red_px"), r.get("fringe_blue_px"))
        if v is not None
    ]
    shading = [
        abs(v)
        for r in corners
        for v in (r.get("rg_vs_centre_pct"), r.get("bg_vs_centre_pct"))
        if v is not None
    ]
    lights = [r["light_vs_centre"] for r in corners if r.get("light_vs_centre") is not None]
    summary = {
        "sheets_seen": len(rows),
        "sheets_missing": [k for k in FIELD_ROLES if k not in by_role],
        "edges_measured": sum(r["edges_measured"] for r in rows),
        "edges_total": EDGES_PER_SHEET * len(FIELD_ROLES),
        "centre_mtf50_cyc_px": c_mtf,
        "corner_mtf50_mean_cyc_px": _r(mean(corner_mtf), 4) if corner_mtf else None,
        "corner_mtf50_worst_cyc_px": worst["mtf50_cyc_px"] if worst else None,
        "corner_worst": worst["role"] if worst else None,
        "corner_vs_centre": _r(_rel(mean(corner_mtf), c_mtf), 3) if corner_mtf else None,
        "corner_spread": (
            _r((max(corner_mtf) - min(corner_mtf)) / mean(corner_mtf), 3)
            if len(corner_mtf) > 1
            else None
        ),
        "sides_mtf50_cyc_px": sides,
        "softer_side": softer,
        "corner_astigmatism": _r(mean(astig), 3) if astig else None,
        "corner_light_vs_centre_min": _r(min(lights), 3) if lights else None,
        "corner_fringe_px_max": _r(max(fringes), 3) if fringes else None,
        "corner_colour_shading_pct_max": _r(max(shading), 2) if shading else None,
        "clipped_sheets": [r["role"] for r in rows if r.get("any_clipped")],
    }
    return {"sheets": rows, "summary": summary}


def field_record(records: Sequence[dict[str, Any]]) -> dict[str, Any] | None:
    """The map as one more chart record (sidecar / ``charts score``), or None."""
    fmap = field_map(records)
    if fmap is None:
        return None
    tags = sorted(t for r in records if r.get("type") == "field" for t in r.get("tag_ids", []))
    return {"chart_id": FIELD_SET_ID, "type": "field_map", "tag_ids": tags, "scores": fmap}


# ------------------------------------------------------------------ plan
#: The boards: three columns of three landscape Letter sheets.
SHEET_W_MM, SHEET_H_MM = 279.4, 215.9
COLUMN_H_MM = 3 * SHEET_H_MM
#: The view's height over the columns' height at the planned distance (tags keep a margin).
PLAN_FIT = 1.08
#: The side boards' outer edges at this fraction of the half width of the view.
PLAN_EDGE_FILL = 0.97
#: Squares smaller than this (px across) measure less reliably.
PLAN_MIN_SQUARE_PX = 120.0
#: Paper edge to the outer square's centre on a sheet (library: FIELD_SQUARE_INSET_MM).
SQUARE_INSET_MM = 42.0


def field_plan(
    pixels: tuple[int, int], pixel_um: float, focal_mm: float, distance_mm: float | None = None
) -> dict[str, Any]:
    """Where the boards go for a camera + lens (thin lens; distance from the lens).

    Without ``distance_mm`` the plan picks the distance at which the columns fill the
    view's height; the side boards then spread to fill its width."""
    sensor_w, sensor_h = pixels[0] * pixel_um / 1000, pixels[1] * pixel_um / 1000
    if distance_mm is None:
        m = sensor_h / (PLAN_FIT * COLUMN_H_MM)
        distance = focal_mm * (1 + 1 / m)
    else:
        if distance_mm <= 2 * focal_mm:
            raise ValueError(f"distance {distance_mm} mm is too close for a {focal_mm} mm lens")
        distance = distance_mm
        m = focal_mm / (distance - focal_mm)
    field_w, field_h = sensor_w / m, sensor_h / m
    px_per_mm = m / (pixel_um / 1000)
    offset = PLAN_EDGE_FILL * field_w / 2 - SHEET_W_MM / 2  # side board centre from the middle
    warnings = []
    if offset < SHEET_W_MM:
        offset = SHEET_W_MM
        warnings.append(
            "the view is narrower than three boards side by side: push the boards together "
            "(the corner squares will not reach the image corners)"
        )
    if field_h < COLUMN_H_MM:
        need = focal_mm * (1 + COLUMN_H_MM * PLAN_FIT / sensor_h)
        warnings.append(
            f"the columns ({COLUMN_H_MM:.0f} mm) are taller than the view ({field_h:.0f} mm): "
            f"the top and bottom sheets are cut off; move back to about {need:.0f} mm"
        )
    square_px = 50.0 * px_per_mm
    if square_px < PLAN_MIN_SQUARE_PX:
        warnings.append(
            f"the 50 mm squares are only {square_px:.0f} px across (≥ {PLAN_MIN_SQUARE_PX:.0f} "
            "measures best): this lens sees too much for Letter sheets at this distance"
        )
    corner = np.array(
        [offset + SHEET_W_MM / 2 - SQUARE_INSET_MM, COLUMN_H_MM / 2 - SQUARE_INSET_MM]
    )
    half = np.array([field_w / 2, field_h / 2])
    corner_r = float(np.linalg.norm(np.minimum(corner, half)) / np.linalg.norm(half))
    return {
        "distance_mm": round(distance),
        "view_mm": [round(field_w), round(field_h)],
        "px_per_mm": round(px_per_mm, 3),
        "square_px": round(square_px),
        "board_centres_mm": [-round(offset), 0, round(offset)],
        "board_gap_mm": round(offset - SHEET_W_MM),
        "outer_edge_to_edge_mm": round(2 * offset + SHEET_W_MM),
        "corner_squares_r": round(corner_r, 2),
        "warnings": warnings,
    }
