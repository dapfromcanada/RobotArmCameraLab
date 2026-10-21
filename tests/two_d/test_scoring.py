"""Tests for two_d.scoring: golden images, determinism, sharpness, noise, config."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pytest
import tifffile
import yaml

from camera_lab.core.config import ConfigError
from camera_lab.core.registry import Registry
from camera_lab.two_d import scoring
from camera_lab.two_d.drivers.simulated import SIM_SERIAL, SimulatedCameraDriver
from camera_lab.two_d.models import CameraSettingsPatch, MetricResult
from camera_lab.two_d.scoring import (
    FractionalROI,
    ScoringParams,
    immerkaer_sigma,
    load_scoring_config,
    score_array,
    score_frame,
    sub_scores,
)


# ------------------------------------------------------------- golden
def test_golden_constant_mid_grey() -> None:
    r = score_array(np.full((64, 64), 128, np.uint8), "Mono8")
    m = 128 / 255
    assert r.metric("mean_level") == round(m, 6)
    assert r.metric("p01") == r.metric("p50") == r.metric("p99") == round(m, 6)
    assert r.metric("clipped_pct") == 0 and r.metric("black_pct") == 0
    assert r.metric("sharpness") == 0 and r.metric("noise_sigma") == 0
    assert r.histogram[128] == 1.0 and sum(r.histogram) == 1.0
    assert len(r.histogram) == 256
    exposure = 1 - abs(m - 0.45) / 0.25
    assert r.sub_scores == pytest.approx(
        {
            "exposure": round(exposure, 6),
            "clipping": 1.0,
            "black": 1.0,
            "noise": 1.0,
        },
        abs=2e-6,
    )
    assert r.composite == pytest.approx(100 * exposure, abs=2e-4)


def test_golden_half_clipped_half_black() -> None:
    img = np.zeros((64, 64), np.uint8)
    img[:, :32] = 255
    r = score_array(img, "Mono8")
    assert r.metric("clipped_pct") == 50.0
    assert r.metric("black_pct") == 50.0
    assert r.metric("mean_level") == 0.5
    assert r.composite == 0.0
    passed = {m.name: m.passed for m in r.metrics}
    assert passed["clipped_pct"] is False and passed["black_pct"] is False
    assert passed["sharpness"] is None


def test_rgb_any_channel_clipped_all_channels_black() -> None:
    img = np.full((10, 10, 3), 100, np.uint8)
    img[0, :] = (255, 0, 0)  # 10 % clipped (one channel at max)
    img[1, :] = (5, 0, 0)  # black: all channels <= 0.02 * 255 = 5.1
    img[2, :] = (6, 0, 0)  # not black
    r = score_array(img, "RGB8")
    assert r.metric("clipped_pct") == 10.0
    assert r.metric("black_pct") == 10.0


def test_mono12_full_scale() -> None:
    img = np.full((8, 8), 2000, np.uint16)
    img[0, 0] = 4095
    r = score_array(img, "Mono12")
    assert r.metric("clipped_pct") == pytest.approx(100 / 64, abs=1e-6)
    r16 = score_array(img, "Mono16")
    assert r16.metric("clipped_pct") == 0.0


def test_bayer_scored_after_demosaic() -> None:
    r = score_array(np.full((16, 16), 255, np.uint8), "BayerRG8")
    assert r.metric("clipped_pct") == 100.0
    assert r.metric("mean_level") == 1.0


def test_composite_formula() -> None:
    p = ScoringParams()
    subs = sub_scores(
        {"mean_level": 0.5, "clipped_pct": 0.5, "black_pct": 1.0, "noise_sigma": 0.015}, p
    )
    assert subs == pytest.approx({"exposure": 0.8, "clipping": 0.5, "black": 0.8, "noise": 0.5})
    worst = sub_scores({"mean_level": 1.0, "clipped_pct": 5, "black_pct": 50, "noise_sigma": 1}, p)
    assert set(worst.values()) == {0.0}


# -------------------------------------------------------- determinism
def test_determinism_same_tiff_identical_json(tmp_path: Path) -> None:
    drv = SimulatedCameraDriver(seed=3)
    cam = drv.open(SIM_SERIAL)
    cam.apply(CameraSettingsPatch(exposure_us=40000, pixel_format="BayerRG8"))
    cam.start_stream()
    frame = cam.grab()
    path = tmp_path / "f.tiff"
    tifffile.imwrite(path, frame.data)
    a = score_array(tifffile.imread(path), "BayerRG8", 8).to_json()
    b = score_array(tifffile.imread(path), "BayerRG8", 8).to_json()
    assert a == b
    assert score_frame(frame).to_json() == a


# ---------------------------------------------------------- sharpness
def test_sharpness_decreases_with_defocus() -> None:
    values = []
    for sigma in (0, 1, 2, 4):
        cam = SimulatedCameraDriver(defocus_sigma=sigma).open(SIM_SERIAL)
        cam.apply(CameraSettingsPatch(exposure_us=40000, pixel_format="Mono8"))
        cam.start_stream()
        values.append(score_frame(cam.grab()).metric("sharpness"))
    assert values == sorted(values, reverse=True)
    assert len(set(values)) == 4


# -------------------------------------------------------------- noise
@pytest.mark.parametrize("sigma", [0.005, 0.01, 0.02])
def test_noise_estimate_within_15pct(sigma: float) -> None:
    rng = np.random.default_rng(42)
    flat = 0.5 + rng.normal(0, sigma, size=(512, 512))
    codes = np.rint(np.clip(flat, 0, 1) * 4095).astype(np.uint16)
    est = score_array(codes, "Mono12").metric("noise_sigma")
    assert est == pytest.approx(sigma, rel=0.15)


def test_immerkaer_needs_3x3() -> None:
    with pytest.raises(ValueError):
        immerkaer_sigma(np.zeros((2, 5)))


# -------------------------------------------------------------- input
@pytest.mark.parametrize(
    ("data", "fmt", "exc"),
    [
        (np.zeros((8, 8), np.float32), "Mono8", TypeError),
        ([[1]], "Mono8", TypeError),
        (np.zeros((8, 8), np.uint8), "Weird8", ValueError),
        (np.zeros((2, 8), np.uint8), "Mono8", ValueError),
    ],
)
def test_bad_input(data: Any, fmt: str, exc: type[Exception]) -> None:
    with pytest.raises(exc):
        score_array(data, fmt)


# -------------------------------------------------------------- config
def test_scoring_yaml_matches_defaults() -> None:
    cfg = load_scoring_config()
    assert cfg.default == ScoringParams()
    assert (cfg.default.target_mean, cfg.default.tolerance, cfg.default.clip_max_pct) == (
        0.45,
        0.25,
        1.0,
    )
    assert (cfg.default.black_max_pct, cfg.default.noise_max, cfg.default.black_threshold) == (
        5.0,
        0.03,
        0.02,
    )


def test_resolve_camera_then_tag(tmp_path: Path) -> None:
    p = tmp_path / "scoring.yaml"
    p.write_text(
        yaml.safe_dump(
            {
                "default": {"target_mean": 0.45},
                "cameras": {"U330439": {"target_mean": 0.40, "noise_max": 0.02}},
                "tags": {"shiny": {"target_mean": 0.35}},
            }
        )
    )
    cfg = load_scoring_config(p)
    assert cfg.resolve().target_mean == 0.45
    jai = cfg.resolve("U330439")
    assert (jai.target_mean, jai.noise_max) == (0.40, 0.02)
    both = cfg.resolve("U330439", "shiny")
    assert (both.target_mean, both.noise_max) == (0.35, 0.02)
    assert cfg.resolve("SIM-0001", "unknown-tag") == cfg.default


@pytest.mark.parametrize(
    "data",
    [
        {"default": {"target_mean": 1.5}},
        {"default": {"roi": {"x": 0.5, "y": 0, "width": 0.6, "height": 1}}},
        {"cameras": {"bad serial": {}}},
        {"default": {"bogus": 1}},
    ],
)
def test_bad_scoring_yaml(tmp_path: Path, data: dict[str, Any]) -> None:
    p = tmp_path / "scoring.yaml"
    p.write_text(yaml.safe_dump(data))
    with pytest.raises(ConfigError):
        load_scoring_config(p)


def test_roi_crops_scoring() -> None:
    img = np.zeros((100, 100), np.uint8)
    img[:, 50:] = 255
    full = score_array(img, "Mono8")
    right = score_array(
        img, "Mono8", params=ScoringParams(roi=FractionalROI(x=0.5, y=0, width=0.5, height=1))
    )
    assert full.metric("clipped_pct") == 50.0
    assert right.metric("clipped_pct") == 100.0
    assert right.params["roi"] == {"x": 0.5, "y": 0.0, "width": 0.5, "height": 1.0}


def test_plugin_metric_is_reported_after_builtins(monkeypatch: pytest.MonkeyPatch) -> None:
    reg: Registry[scoring.MetricFn] = Registry("metric")
    for name in scoring.METRICS.names():
        reg.register(name)(scoring.METRICS.get(name))
    reg.register("max_code")(
        lambda ctx: MetricResult(name="max_code", value=float(ctx.codes.max()))
    )
    monkeypatch.setattr(scoring, "METRICS", reg)
    r = score_array(np.full((8, 8), 7, np.uint8), "Mono8")
    assert [m.name for m in r.metrics][-1] == "max_code"
    assert r.metric("max_code") == 7.0
