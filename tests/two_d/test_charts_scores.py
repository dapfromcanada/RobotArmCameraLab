"""Phase 10 step 3: chart scores against known synthetic targets."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import cv2
import numpy as np
import pytest
import tifffile
from typer.testing import CliRunner

from camera_lab.cli import app
from camera_lab.two_d.charts import builtin_charts, init_builtin
from camera_lab.two_d.charts.detect import ChartDetection, charts_from_tags, detect_tags
from camera_lab.two_d.charts.draw import rasterize
from camera_lab.two_d.charts.models import ChartSpec
from camera_lab.two_d.charts.scores import (
    chart_records_array,
    score_grey_patch,
    score_resolution,
    score_step_wedge,
)
from camera_lab.two_d.models import Frame
from camera_lab.two_d.service import TwoDService

SIM = "SIM-0001"
WHITE = 0.9


def _chart(kind: str) -> ChartSpec:
    return next(c for c in builtin_charts() if c.type == kind)


def _detect(img8: np.ndarray, chart: ChartSpec) -> ChartDetection:
    [det] = charts_from_tags(detect_tags(img8), {t.id: chart for t in chart.tags}).charts
    return det


def _expected_mtf50(sigma: float) -> float:
    """Gaussian blur × pixel aperture (sinc): MTF50 in cycles/pixel."""
    f = np.linspace(1e-4, 1.0, 200_000)
    m = np.exp(-2 * np.pi**2 * sigma**2 * f**2) * np.abs(np.sinc(f))
    return float(f[np.argmax(m < 0.5)])


def _render(kind: str, px: float, sigma: float = 0.0) -> tuple[np.ndarray, ChartSpec]:
    """8-bit render; paper white at 90 % of full scale (a sensible exposure, not clipped)."""
    chart = _chart(kind)
    img = rasterize(chart, px).astype(np.float64) * WHITE
    if sigma:
        img = cv2.GaussianBlur(img, (0, 0), sigma)
    return np.clip(np.round(img), 0, 255).astype(np.uint8), chart


# ------------------------------------------------------------- resolution
@pytest.mark.parametrize(("px", "sigma"), [(4.0, 1.0), (6.0, 1.5), (3.0, 0.7), (4.0, 2.0)])
def test_mtf50_matches_theory(px: float, sigma: float) -> None:
    img, chart = _render("resolution", px, sigma)
    r = score_resolution(img / 255.0, _detect(img, chart), chart)
    expected = _expected_mtf50(sigma)
    assert r["edges_measured"] == 8
    assert r["mtf50_cyc_px"] == pytest.approx(expected, rel=0.04)
    assert r["mtf50_lp_mm"] == pytest.approx(expected * px, rel=0.05)
    assert r["mtf50_horizontal_cyc_px"] == pytest.approx(r["mtf50_vertical_cyc_px"], rel=0.03)
    assert not r["any_clipped"]


@pytest.mark.parametrize(("px", "sigma"), [(3.5, 1.0), (9.8, 1.9)])
def test_mtf50_holds_on_a_noisy_frame(px: float, sigma: float) -> None:
    """Bench frames are noisy and the chart may be close (wide edge regions): the noise
    across a wide region must not pull the edge off (2026-10-28: TRI089S-M read 0.03 for a
    real ~0.09 at 9.8 px/mm, and ~35 % low at 3.5 px/mm)."""
    chart = _chart("resolution")
    page = cv2.GaussianBlur(rasterize(chart, px).astype(np.float64) / 255.0, (0, 0), sigma)
    rng = np.random.default_rng(7)
    img = 0.06 + 0.5 * page + rng.normal(0.0, 0.004, page.shape)  # bench-like levels and noise
    det = _detect(np.clip(np.round(page * 255), 0, 255).astype(np.uint8), chart)
    r = score_resolution(img, det, chart)
    assert r["edges_measured"] == 8
    assert r["mtf50_cyc_px"] == pytest.approx(_expected_mtf50(sigma), rel=0.05)


def test_squares_are_found_where_the_tag_misplaces_them() -> None:
    """A chart close to a wide lens: the squares sit tens of pixels from where the one tag
    predicts them. The edges are placed from the squares found in the image."""
    px, sigma = 9.8, 1.9
    chart = _chart("resolution")
    page = rasterize(chart, px).astype(np.float64) / 255.0
    top = int(120 * px)  # the squares are below 120 mm, the tag above
    page[top:] = np.roll(page[top:], (25, 30), axis=(0, 1))  # ~3 mm off the prediction
    page = cv2.GaussianBlur(page, (0, 0), sigma)
    rng = np.random.default_rng(3)
    img = 0.06 + 0.5 * page + rng.normal(0.0, 0.004, page.shape)
    det = _detect(np.clip(np.round(page * 255), 0, 255).astype(np.uint8), chart)
    r = score_resolution(img, det, chart)
    assert r["edges_measured"] == 8
    assert {e["placed_by"] for e in r["edges"]} == {"square"}
    assert r["mtf50_cyc_px"] == pytest.approx(_expected_mtf50(sigma), rel=0.05)


def test_mtf50_on_a_tilted_view() -> None:
    chart = _chart("resolution")
    page = rasterize(chart, 4.0)
    h, w = page.shape
    src = np.float32([[0, 0], [w, 0], [w, h], [0, h]])
    dst = np.float32([[40, 30], [w + 20, 50], [w, h + 60], [30, h + 40]])
    warp = cv2.getPerspectiveTransform(src, dst)
    img = cv2.warpPerspective(page, warp, (w + 80, h + 120), borderValue=255).astype(np.float64)
    img = np.clip(np.round(cv2.GaussianBlur(img, (0, 0), 1.2)), 0, 255).astype(np.uint8)
    r = score_resolution(img / 255.0, _detect(img, chart), chart)
    assert r["edges_measured"] == 8
    # resampling by the warp adds a little blur: measured ≤ theory, within 10 %
    assert r["mtf50_cyc_px"] == pytest.approx(_expected_mtf50(1.2), rel=0.10)


def test_harder_blur_gives_lower_resolution() -> None:
    sharp, chart = _render("resolution", 4.0, 0.8)
    soft, _ = _render("resolution", 4.0, 2.5)
    a = score_resolution(sharp / 255.0, _detect(sharp, chart), chart)["mtf50_lp_mm"]
    b = score_resolution(soft / 255.0, _detect(soft, chart), chart)["mtf50_lp_mm"]
    assert a > 2 * b


def test_edges_outside_the_image_are_reported_not_measured() -> None:
    img, chart = _render("resolution", 4.0, 1.0)
    det = _detect(img, chart)
    cropped = img[:, : img.shape[1] // 2]  # right-hand square cut away
    r = score_resolution(cropped / 255.0, det, chart)
    errors = [e for e in r["edges"] if "error" in e]
    assert r["edges_measured"] < 8 and errors
    assert all(e["edge"].startswith("edge-right") for e in errors)


def test_clipped_edge_is_flagged() -> None:
    img, chart = _render("resolution", 4.0, 1.0)
    r = score_resolution(np.minimum(img / 255.0 * 1.3, 1.0), _detect(img, chart), chart)
    assert r["any_clipped"]


# ------------------------------------------------------------- step wedge
def test_step_wedge_clean_print() -> None:
    img, chart = _render("step_wedge", 3.0)
    r = score_step_wedge(img / 255.0, _detect(img, chart), chart)
    assert r["steps_seen"] == 20 and r["steps_resolved"] == 20
    assert r["clipped_steps"] == 0 and r["monotonic"]
    assert r["r2_vs_nominal_grey"] == pytest.approx(1.0, abs=1e-4)
    assert r["levels"][0] == pytest.approx(0.0, abs=0.01)
    assert r["levels"][-1] == pytest.approx(WHITE, abs=0.01)


def test_step_wedge_noise_and_clipping() -> None:
    img, chart = _render("step_wedge", 3.0)
    det = _detect(img, chart)
    noisy = np.clip(img / 255.0 + np.random.default_rng(1).normal(0, 0.05, img.shape), 0, 1)
    r = score_step_wedge(noisy, det, chart)
    assert r["steps_resolved"] < 20  # adjacent steps (Δ ≈ 0.053) are within 2 σ of the noise
    over = np.minimum(img / 255.0 * 1.6, 1.0)  # white at 1.44 → clipped
    r = score_step_wedge(over, det, chart)
    clip_from = int(np.ceil(0.98 / (WHITE * 1.6) * 19))  # first step index at ≥ 0.98
    assert r["clipped_steps"] == 20 - clip_from
    assert r["steps_resolved"] == clip_from


# ------------------------------------------------------------- grey patch
def test_grey_patch_mono_and_colour_cast() -> None:
    img, chart = _render("grey_patch", 3.0)
    det = _detect(img, chart)
    lifted = img / 255.0 + 0.05  # black reference no longer 0
    r = score_grey_patch(lifted, det, chart)
    assert r["level"] == pytest.approx(0.05 + round(WHITE * 128) / 255, abs=0.005)
    assert r["wb_spread_pct"] is None and r["channel_means"] is None
    assert r["black_level"] == pytest.approx(0.05, abs=0.005)
    assert r["contrast_vs_black"] == pytest.approx(r["level"] / 0.05, rel=0.05)
    rgb = np.repeat(lifted[..., None], 3, axis=2)
    rgb[..., 0] *= 1.1
    r = score_grey_patch(rgb, det, chart)
    g = r["channel_means"]["green"]
    assert r["wb_spread_pct"] == pytest.approx(0.1 * g / ((1.1 * g + 2 * g) / 3) * 100, rel=0.03)
    assert r["channel_means"]["red"] > r["channel_means"]["green"]


# ------------------------------------------------------- records, service
def test_records_include_scores_except_for_tag_cards() -> None:
    a, wedge = _render("step_wedge", 2.5)
    b, card = _render("tag_card", 2.5)
    img = np.full((a.shape[0], a.shape[1] * 2 + 40), 255, np.uint8)
    img[:, : a.shape[1]] = a
    img[:, a.shape[1] + 40 :] = b
    lib = {t.id: c for c in (wedge, card) for t in c.tags}
    recs = chart_records_array(img, "Mono8", 8, lib)
    by = {r["chart_id"]: r for r in recs}
    assert by["step-wedge-letter-v1"]["scores"]["steps_resolved"] == 20
    assert "scores" not in by["tag-card-letter-v1"]


def test_score_charts_file_is_deterministic_and_cli(
    make_service: Callable[..., TwoDService], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    init_builtin(tmp_path / "charts")
    img, _ = _render("resolution", 4.0, 1.0)
    path = tmp_path / "res.tiff"
    tifffile.imwrite(path, img)
    svc = make_service(charts_root=tmp_path / "charts")
    first = json.dumps(svc.score_charts_file(path), sort_keys=True)
    assert first == json.dumps(svc.score_charts_file(path), sort_keys=True)
    rec = json.loads(first)[0]
    assert rec["chart_id"] == "resolution-letter-v1" and rec["scores"]["edges_measured"] == 8

    monkeypatch.setattr("camera_lab.two_d.charts.library.charts_dir", lambda: tmp_path / "charts")
    monkeypatch.setattr("camera_lab.cli_support.build_service", lambda sim=False: svc)
    res = CliRunner().invoke(app, ["charts", "score", str(path)])
    assert res.exit_code == 0, res.output
    assert "resolution-letter-v1" in res.stdout and "mtf50_cyc_px" in res.stdout


def test_capture_sidecar_has_chart_scores(
    make_service: Callable[..., TwoDService], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    init_builtin(tmp_path / "charts")
    svc = make_service(charts_root=tmp_path / "charts")
    svc.open(SIM, "test")
    real = svc.grab(SIM)
    page, _ = _render("grey_patch", 2.0)

    def grab(camera_id: str, timeout_ms: int = 2000) -> Frame:
        return real.model_copy(update={"data": page, "pixel_format": "Mono8", "bit_depth": 8})

    monkeypatch.setattr(svc, "grab", grab)
    [res] = svc.capture(SIM, 1)
    assert res.charts[0]["scores"]["level"] == pytest.approx(WHITE * 128 / 255, abs=0.01)
    assert res.preview_path is not None
    sidecar = json.loads((res.preview_path.parent.parent / "captures" / "0001.json").read_text())
    assert sidecar["charts"][0]["scores"]["snr"] > 1000  # a noiseless render
