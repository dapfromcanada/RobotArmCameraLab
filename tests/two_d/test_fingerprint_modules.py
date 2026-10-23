"""Phase 11: fingerprint modules by sensor type (core, colour, HDR) and the colour planes."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import numpy as np
import pytest

from camera_lab.two_d.drivers.simulated import SimulatedCameraDriver
from camera_lab.two_d.fingerprint import measure as fm
from camera_lab.two_d.fingerprint.session import FingerprintSession
from camera_lab.two_d.service import TwoDService

SIM = "SIM-0001"


@pytest.mark.parametrize(
    ("pattern", "sites"),
    [
        ("BayerRG12p", ("R", "Gr", "Gb", "B")),
        ("BayerGR12", ("Gr", "R", "B", "Gb")),
        ("BayerGB8", ("Gb", "B", "R", "Gr")),
        ("BayerBG16", ("B", "Gb", "Gr", "R")),
    ],
)
def test_bayer_sites_and_raw_green(pattern: str, sites: tuple[str, ...]) -> None:
    tile = np.array([[1, 2], [3, 4]])
    data = np.tile(tile, (4, 4))
    planes = fm.colour_planes(data, pattern)
    assert {name: int(planes[name][0, 0]) for name in planes} == dict(
        zip(sites, (1, 2, 3, 4), strict=True)
    )
    # the raw plane is the green on the red rows, whatever the pattern
    assert int(fm.raw_plane(data, pattern)[0, 0]) == 1 + sites.index("Gr")


def test_prism_planes_follow_the_channel_order() -> None:
    data = np.dstack([np.full((4, 4), v) for v in (10, 20, 30)])
    assert {k: int(v[0, 0]) for k, v in fm.colour_planes(data, "RGB12").items()} == {
        "R": 10,
        "G": 20,
        "B": 30,
    }
    assert int(fm.colour_planes(data, "BGR8")["R"][0, 0]) == 30
    assert fm.colour_planes(np.zeros((4, 4)), "Mono12") == {}


def test_colour_result_recovers_gains_noise_and_imbalance() -> None:
    rng = np.random.default_rng(3)
    black, green = 100.0, 1000.0
    gains = {"R": 0.6, "Gr": 1.01, "Gb": 0.99, "B": 0.45}
    prnu = {"R": 0.02, "Gr": 0.01, "Gb": 0.01, "B": 0.03}
    shape = (200, 200)
    fixed = {k: 1 + rng.normal(0, prnu[k], shape) for k in gains}
    frames = []
    for _ in range(6):
        img = np.zeros((400, 400))
        for name, (dy, dx) in zip(
            ("R", "Gr", "Gb", "B"), ((0, 0), (0, 1), (1, 0), (1, 1)), strict=True
        ):
            sig = green * gains[name] * fixed[name]
            img[dy::2, dx::2] = black + sig + rng.normal(0, 5.0, shape)
        frames.append(img)
    res = fm.colour_result(frames, "BayerRG12", black)
    planes = {p.name: p for p in res.planes}
    assert planes["R"].relative_to_green == pytest.approx(0.6, rel=0.01)
    assert planes["B"].relative_to_green == pytest.approx(0.45, rel=0.01)
    assert res.green_imbalance_pct == pytest.approx(2.0, abs=0.2)
    assert planes["B"].temporal_sigma_dn == pytest.approx(5.0, rel=0.05)
    for name in gains:
        assert planes[name].prnu_pct == pytest.approx(prnu[name] * 100, rel=0.15), name
    with pytest.raises(ValueError, match="mono"):
        fm.colour_result([np.zeros((4, 4))] * 2, "Mono12", 0)


@pytest.fixture
def ses(make_service: Callable[..., TwoDService], tmp_path: Path) -> FingerprintSession:
    svc = make_service(drivers={"simulated": SimulatedCameraDriver(None)})
    svc.open(SIM, "test")
    return FingerprintSession(svc, SIM, root=tmp_path / "fp")


def _replan(ses: FingerprintSession, colour: str, hdr: bool) -> None:
    ses.colour, ses.hdr_capable, ses.recipe = colour, hdr, None
    ses.plan = ses._build_plan()


def _rows(ses: FingerprintSession) -> list[str]:
    return [s for s, p in ses.plan.items() if p.active]


def test_modules_follow_the_sensor_type(ses: FingerprintSession) -> None:
    _replan(ses, "mono", hdr=False)  # e.g. the TRI089S-M: core only
    assert _rows(ses) == ["cap", "diffuser", "finish"]
    assert ses.plan["diffuser"].measures == ("timing", "flat", "noise_gain")
    assert "colour" not in ses.plan["diffuser"].instruction
    assert ses.plan["finish"].label == "3. Finish" and ses.plan["finish"].checks == ()

    _replan(ses, "prism", hdr=False)  # e.g. the JAI: core + colour
    assert _rows(ses) == ["cap", "diffuser", "finish"]
    assert ses.plan["diffuser"].measures == ("timing", "flat", "colour", "noise_gain")
    assert "the colour planes" in ses.plan["diffuser"].instruction

    _replan(ses, "bayer", hdr=True)  # e.g. the TRI054S without a recipe: core + colour + HDR
    assert _rows(ses) == ["cap", "diffuser", "light_2", "finish"]
    assert ses.plan["light_2"].measures == ("channels_a",)
    assert ses.plan["finish"].label == "4. Iris back open: finish"


def test_bayer_camera_measures_its_colour_planes(
    make_service: Callable[..., TwoDService], tmp_path: Path
) -> None:
    """End to end on the simulated Bayer sensor (8-bit only: the run warns about it)."""
    from camera_lab.two_d.drivers.simulated import SensorModel

    drv = SimulatedCameraDriver(None, sensor=SensorModel(prnu=0.01), black_level=0.05)
    svc = make_service(drivers={"simulated": drv}, backups_root=tmp_path / "backups")
    svc.open(SIM, "test")
    ses = FingerprintSession(svc, SIM, root=tmp_path / "fp")
    cam = svc._open[SIM].camera
    cam.scene = "cap"  # type: ignore[attr-defined]
    cap = ses.run("cap", ses.plan["cap"].checks)
    assert cap.ok, cap.message
    assert ses.fmt == "BayerRG8" and "only 8-bit raw" in cap.message
    cam.scene = "flat"  # type: ignore[attr-defined]
    dif = ses.run("diffuser", ses.plan["diffuser"].checks)
    assert dif.ok, dif.message
    colour = dif.data["colour"]
    assert set(colour["relative_to_green"]) == {"R", "Gr", "Gb", "B"}
    assert colour["green_imbalance_pct"] is not None
    assert len(colour["repeats"]["relative_to_green.R"]["values"]) == 3
    assert "colour planes: R/G" in dif.message


def test_row_variance_separates_banding_from_pixel_noise() -> None:
    rng = np.random.default_rng(7)
    shape = (300, 400)
    a, b = rng.normal(1000, 20, shape), rng.normal(1000, 20, shape)
    assert fm.row_variance(a, b) == pytest.approx(0, abs=2.0)  # pixel noise only
    rows = np.arange(300)[:, None]
    band_a = a + 30 * np.sin(2 * np.pi * rows / 5)
    band_b = b + 30 * np.sin(2 * np.pi * rows / 5 + np.pi)  # the ripple moved between frames
    # (a - b)/√2 carries 60·sin/√2: row variance 60² / 2 / 2 = 900
    assert fm.row_variance(band_a, band_b) == pytest.approx(900, rel=0.1)
    pts = [fm.PtcPoint(exposure_us=1000, mean_dn=1000, var_dn2=400, row_var_dn2=900)]
    assert fm.flicker_inflation(pts, 0.0) == (pytest.approx(2.25), 1000)
    assert fm.flicker_inflation(pts, 900.0)[0] == 0.0  # all of it is the sensor's row noise
