"""Phase 10 step 7: the field chart (whole image in one shot), its plan and the Focus tool."""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pytest
import tifffile
from typer.testing import CliRunner

from camera_lab.cli import app
from camera_lab.two_d.charts import field_charts
from camera_lab.two_d.charts.field import field_plan
from camera_lab.two_d.charts.scores import chart_records_array
from camera_lab.two_d.focus import FocusMeter
from camera_lab.two_d.service import TwoDService

from . import field_board as fb

LIB = {c.tags[0].id: c for c in field_charts()}
SIM = "SIM-0001"
runner = CliRunner()


def _map(img: np.ndarray, fmt: str = "Mono8") -> dict:
    recs = chart_records_array(fb.to8(img), fmt, 8, LIB)
    return next(r for r in recs if r["type"] == "field_map")["scores"]


def _expected_mtf50(sigma: float) -> float:
    f = np.linspace(1e-4, 1.0, 200_000)
    m = np.exp(-2 * np.pi**2 * sigma**2 * f**2) * np.abs(np.sinc(f))
    return float(f[np.argmax(m < 0.5)])


@pytest.fixture(scope="module")
def soft_corners() -> dict:
    """Blur growing with the field radius, extra blur across the side sheets' tangential
    edges, light falling off toward the corners."""
    img = fb.board(sigma=lambda r: 0.8 + 1.2 * r, side_extra_x=1.5, light=lambda r: 1 - 0.4 * r * r)
    return _map(img)


def test_every_sheet_and_edge_is_measured(soft_corners: dict) -> None:
    s = soft_corners["summary"]
    assert s["sheets_seen"] == 9 and s["sheets_missing"] == []
    assert s["edges_measured"] == s["edges_total"] == 72
    roles = [r["role"] for r in soft_corners["sheets"]]
    assert roles[0] == "centre" and len(set(roles)) == 9


def test_sharpness_falls_toward_the_corners(soft_corners: dict) -> None:
    rows = {r["role"]: r for r in soft_corners["sheets"]}
    s = soft_corners["summary"]
    assert rows["centre"]["r"] < 0.2 and all(
        rows[k]["r"] > 0.7 for k in ("top-left", "bottom-right")
    )
    assert s["centre_mtf50_cyc_px"] > rows["top"]["mtf50_cyc_px"] > s["corner_mtf50_mean_cyc_px"]
    assert s["corner_vs_centre"] == pytest.approx(0.55, abs=0.08)
    assert s["corner_spread"] < 0.03 and s["softer_side"] is None


def test_sagittal_and_tangential_are_told_apart(soft_corners: dict) -> None:
    rows = {r["role"]: r for r in soft_corners["sheets"]}
    for side in ("left", "right"):  # x blur: their vertical (tangential) edges are softer
        assert rows[side]["mtf50_tangential_cyc_px"] < 0.8 * rows[side]["mtf50_sagittal_cyc_px"]
    for corner in ("top-left", "bottom-right"):  # round blur: no astigmatism
        r = rows[corner]
        assert r["mtf50_sagittal_cyc_px"] == pytest.approx(r["mtf50_tangential_cyc_px"], rel=0.05)
    assert rows["centre"]["mtf50_horizontal_cyc_px"] or rows["centre"]["mtf50_tangential_cyc_px"]


def test_light_toward_the_corners(soft_corners: dict) -> None:
    rows = {r["role"]: r for r in soft_corners["sheets"]}
    assert rows["centre"]["light_vs_centre"] == 1.0
    assert soft_corners["summary"]["corner_light_vs_centre_min"] == pytest.approx(0.84, abs=0.03)
    assert rows["top"]["light_vs_centre"] > rows["top-left"]["light_vs_centre"]


def test_mtf50_matches_theory_on_every_sheet() -> None:
    sigma = 1.0
    fmap = _map(fb.board(sigma=lambda r: sigma, px=3.0))
    for row in fmap["sheets"]:
        assert row["mtf50_cyc_px"] == pytest.approx(_expected_mtf50(sigma), rel=0.05), row["role"]


def test_a_tilted_lens_shows_as_a_softer_side() -> None:
    fmap = _map(fb.board(sigma=lambda r: 0.9, left_extra=1.2))
    s = fmap["summary"]
    assert s["softer_side"] == "left"
    assert s["corner_worst"] in ("top-left", "bottom-left") and s["corner_spread"] > 0.2


def test_missing_and_misplaced_sheets() -> None:
    img = fb.board(missing=("top-right",), shift_px={"bottom-left": (-20, 15), "top": (30, 0)})
    s = _map(img)["summary"]
    assert s["sheets_missing"] == ["top-right"] and s["sheets_seen"] == 8
    assert s["edges_measured"] == 64


def test_colour_fringing_and_shading() -> None:
    mono = fb.board(sigma=lambda r: 0.9)
    rgb = fb.colour(mono, red_scale=1.001)  # red lands 0.1 % further out: ~1 px at a corner
    h, w = mono.shape
    fmap = _map(rgb, "RGB8")
    for row in fmap["sheets"]:
        if row["role"] in ("top-left", "bottom-right"):
            expect = 0.001 * row["r"] * math.hypot(w, h) / 2
            assert row["fringe_red_px"] == pytest.approx(expect, abs=0.25)
            assert abs(row["fringe_blue_px"]) < 0.15
    assert fmap["summary"]["corner_fringe_px_max"] > 0.6
    tint = rgb.copy()
    yy, xx = np.mgrid[0:h, 0:w]
    rr = np.hypot(xx - w / 2, yy - h / 2) / (math.hypot(w, h) / 2)
    tint[..., 0] *= 1 + 0.08 * rr**2  # red cast toward the corners
    shade = _map(tint, "RGB8")
    rows = {r["role"]: r for r in shade["sheets"]}
    assert (
        rows["top-left"]["rg_vs_centre_pct"] > 3 and abs(rows["top-left"]["bg_vs_centre_pct"]) < 1
    )
    assert shade["summary"]["corner_colour_shading_pct_max"] > 3


def test_scores_are_deterministic() -> None:
    img = fb.board(sigma=lambda r: 1.0 + r)
    assert json.dumps(_map(img), sort_keys=True) == json.dumps(_map(img), sort_keys=True)


# ------------------------------------------------------------------ plan
def test_plan_for_the_mono_and_computar() -> None:
    plan = field_plan((4096, 2160), 3.45, 8.0)
    assert plan["distance_mm"] == pytest.approx(760, abs=10)
    assert plan["view_mm"][1] > 3 * 215.9 and plan["warnings"] == []
    assert plan["board_gap_mm"] > 0 and plan["corner_squares_r"] > 0.85
    assert plan["square_px"] > 120


def test_plan_warns_when_the_boards_do_not_fit() -> None:
    close = field_plan((4096, 2160), 3.45, 8.0, distance_mm=650)
    assert any("taller than the view" in w for w in close["warnings"])
    far = field_plan((4096, 2160), 3.45, 8.0, distance_mm=2000)
    assert any("only" in w and "px across" in w for w in far["warnings"])
    square = field_plan((1000, 1000), 3.45, 8.0)
    assert any("narrower" in w for w in square["warnings"])
    with pytest.raises(ValueError, match="too close"):
        field_plan((4096, 2160), 3.45, 8.0, distance_mm=10)


def test_cli_field_plan_and_field_map(tmp_path: Path) -> None:
    res = runner.invoke(
        app,
        [
            *("charts", "field-plan", "--model", "NO-SUCH", "--focal-mm", "8"),
            *("--pixel-um", "3.45", "--width-px", "4096", "--height-px", "2160"),
        ],
    )
    assert res.exit_code == 0, res.output
    assert "distance (lens to boards)" in res.stdout
    res = runner.invoke(app, ["charts", "field-plan", "--model", "NO-SUCH", "--focal-mm", "8"])
    assert res.exit_code == 2 and "missing" in res.output
    tiff = tmp_path / "board.tiff"
    tifffile.imwrite(tiff, fb.to8(fb.board()))
    res = runner.invoke(app, ["--sim", "charts", "field", str(tiff)])
    assert res.exit_code == 0, res.output
    assert "top-left" in res.stdout and "corners/centre" in res.stdout
    blank = tmp_path / "blank.tiff"
    tifffile.imwrite(blank, np.full((200, 300), 200, np.uint8))
    assert runner.invoke(app, ["--sim", "charts", "field", str(blank)]).exit_code == 2


# ------------------------------------------------------------ focus tool
def test_focus_tool_reads_the_centre_sheet_and_the_corners(svc: TwoDService) -> None:
    real = svc.grab(SIM)
    img = fb.to8(fb.board(sigma=lambda r: 0.9, left_extra=1.2))
    frame = real.model_copy(update={"data": img, "pixel_format": "Mono8", "bit_depth": 8})
    r = FocusMeter(LIB).read(frame, 10_000.0)
    assert r.found and r.edges == 8 and r.main is not None
    assert r.corner_worst is not None and r.corner_worst[0] in ("top-left", "bottom-left")
    assert r.softer_side == "left"
    assert set(r.field_mtf50) == {c.role for c in field_charts()}
