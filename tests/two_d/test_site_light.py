"""Site light assessment: verdicts against a camera model's bench baseline (simulated camera)."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from camera_lab.two_d.camera_models import load_model_sheet
from camera_lab.two_d.drivers.simulated import SensorModel, SimulatedCameraDriver
from camera_lab.two_d.models import CameraSettingsPatch
from camera_lab.two_d.service import TwoDService
from camera_lab.two_d.site.light import assess_light

SIM = "SIM-0001"
BLACK = 0.05 * 4095
ROW_TIME_US = 1e6 / (192 * 120)  # a 192-row band period is 120 Hz on this "sensor"
SHEET = f"""
model: Simulated2D
sensor: {{shutter: rolling}}
design_mode: linear
timing: {{row_time_us: {ROW_TIME_US}}}
performance:
  black_dn: {BLACK}
  read_noise_dn: 2.0
  row_noise_dn: 0.0
  dynamic_range_stops: 11
  snr_vs_signal:
    points: {{0.02: 10, 0.1: 30, 0.3: 50, 0.6: 70}}
state_checks:
  linear_output: {{expect_ratio: [1.9, 2.1], lut_on_ratio: 1.41}}
"""


@pytest.fixture
def sheets(tmp_path: Path) -> Path:
    root = tmp_path / "models"
    root.mkdir()
    (root / "Simulated2D.yaml").write_text(SHEET)
    return root


@pytest.fixture
def svc(make_service: Callable[..., TwoDService], tmp_path: Path) -> TwoDService:
    drv = SimulatedCameraDriver(None, sensor=SensorModel(), black_level=0.05)
    s = make_service(drivers={"simulated": drv}, backups_root=tmp_path / "backups")
    s.open(SIM, "test")
    s.apply(SIM, CameraSettingsPatch(pixel_format="Mono12", exposure_us=5000))
    s._open[SIM].camera.scene = "flat"  # type: ignore[attr-defined]
    return s


def _by(rep: Any) -> dict[str, Any]:
    return {c.name: c for c in rep.checks}


def _assess(svc: TwoDService, sheets: Path, tmp_path: Path, **kw: Any) -> Any:
    return assess_light(svc, SIM, sheet_root=sheets, out_root=tmp_path / "out", **kw)


def test_sheet_loads_and_interpolates(sheets: Path) -> None:
    sheet = load_model_sheet("Simulated2D", sheets)
    assert sheet is not None and sheet.sensor.shutter == "rolling"
    snr = sheet.performance.snr_vs_signal
    assert snr is not None and snr.best_level() == 0.6
    assert snr.at(0.2) == pytest.approx(40)
    assert load_model_sheet("../etc/passwd", sheets) is None
    assert load_model_sheet("Unknown", sheets) is None
    real = load_model_sheet("TRI054S-C")  # the bench white paper's sheet
    assert real is not None and real.timing.row_time_us == 23.5


def test_a_good_scene_passes_and_is_restored(
    svc: TwoDService, sheets: Path, tmp_path: Path
) -> None:
    before = svc.get_settings(SIM).settings
    rep = _assess(svc, sheets, tmp_path)
    c = _by(rep)
    assert c["camera_state"].status == "pass"
    assert c["camera_state"].data["ratio"] == pytest.approx(2.0, abs=0.05)
    assert c["flicker"].status == "pass"
    assert c["brightness"].data["target_level"] == 0.6  # the model's best-SNR level
    assert c["snr"].status in ("pass", "info")
    assert c["distribution"].status == "info"
    assert rep.recommended_exposure_us and rep.baseline.endswith("Simulated2D.yaml")
    after = svc.get_settings(SIM).settings
    assert after.exposure_us == before.exposure_us  # restored
    folder = Path(rep.folder)
    assert json.loads((folder / "report.json").read_text())["verdict"] == rep.verdict
    assert "Site light assessment" in (folder / "report.md").read_text()


def test_apply_leaves_the_recommended_exposure(
    svc: TwoDService, sheets: Path, tmp_path: Path
) -> None:
    rep = _assess(svc, sheets, tmp_path, apply=True)
    assert svc.get_settings(SIM).settings.exposure_us == pytest.approx(
        rep.recommended_exposure_us, rel=0.01
    )


def _wrap_grab(svc: TwoDService, monkeypatch: pytest.MonkeyPatch, fn: Callable[..., Any]) -> None:
    real = svc.grab

    def grab(camera_id: str, timeout_ms: int = 2000) -> Any:
        f = real(camera_id, timeout_ms)
        return f.model_copy(update={"data": fn(f.data.astype(np.float64)).astype(f.data.dtype)})

    monkeypatch.setattr(svc, "grab", grab)


def test_a_gamma_lut_is_caught(
    svc: TwoDService, sheets: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _wrap_grab(svc, monkeypatch, lambda d: np.sqrt(np.clip(d, 0, None) / 4095) * 4095)
    c = _by(_assess(svc, sheets, tmp_path))
    assert c["camera_state"].status == "fail"
    assert "a gamma LUT is on" in c["camera_state"].message
    assert "design mode (linear)" in c["camera_state"].action


def test_mains_flicker_is_recognised(
    svc: TwoDService, sheets: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rng = np.random.default_rng(4)
    rows = np.arange(768)[:, None]

    def ripple(d: np.ndarray) -> np.ndarray:
        phase = rng.uniform(0, 2 * np.pi)
        sig = d - BLACK
        return BLACK + sig * (1 + 0.04 * np.sin(2 * np.pi * rows / 192 + phase))

    _wrap_grab(svc, monkeypatch, ripple)
    rep = _assess(svc, sheets, tmp_path)
    fl = _by(rep)["flicker"]
    assert fl.status in ("warn", "fail")
    assert fl.data["ripple_hz"] == pytest.approx(120, rel=0.05)
    assert "mains flicker" in fl.message and "60 Hz mains" in fl.message
    assert "multiples of 8.33 ms" in fl.action
    rec = rep.recommended_exposure_us
    assert rec is not None and (rec / 8333.33) == pytest.approx(round(rec / 8333.33), abs=0.01)


def test_too_little_light_for_the_budget(svc: TwoDService, sheets: Path, tmp_path: Path) -> None:
    rep = _assess(svc, sheets, tmp_path, max_exposure_us=300.0)
    br = _by(rep)["brightness"]
    assert br.status in ("warn", "fail")
    assert br.message.startswith("too dark")
    assert "more light" in br.action and "never more gain" in br.action
    assert rep.verdict in ("warn", "fail")


def test_snr_by_level_on_a_textured_scene() -> None:
    """Per-band SNR follows the noise model across a textured scene; static edges cancel and a
    few changing pixels (glints, dust) do not count as noise. (Moving edges — vibration — do:
    the check's advice says so.)"""
    from camera_lab.two_d.fingerprint import measure as fm

    rng = np.random.default_rng(9)
    span, black, k, rn = 4000.0, 100.0, 0.5, 3.0  # DN/e-, read noise DN
    scene = np.geomspace(4, 3000, 400)[None, :] * np.ones((300, 1))  # a ramp of levels
    scene[:, ::40] = 3500  # thin bright lines: static edges

    def frame(shift: int) -> np.ndarray:
        img = black + scene + rng.normal(0, np.sqrt(k * scene + rn**2))
        glints = rng.random(scene.shape) < 0.005  # 0.5 % of pixels flicker (glints, dust)
        img[glints] += rng.uniform(-500, 500, int(glints.sum()))
        return img

    bands = fm.snr_by_level(frame(0), frame(1), black, span, min_pixels=200)
    assert len(bands) >= 6
    for band in bands:
        sig = band.level * span
        model = sig / np.sqrt(k * sig + rn**2)
        assert band.snr == pytest.approx(model, rel=0.15), band
