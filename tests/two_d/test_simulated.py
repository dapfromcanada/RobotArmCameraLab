"""Tests for the simulated camera."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import numpy as np
import pytest

from camera_lab.core.lights import SimulatedLight
from camera_lab.two_d.drivers.simulated import (
    SIM_SERIAL,
    SimulatedCamera,
    SimulatedCameraDriver,
)
from camera_lab.two_d.errors import (
    CameraClosedError,
    CameraNotFoundError,
    CaptureTimeoutError,
    IncompleteFrameError,
    StreamingStateError,
)
from camera_lab.two_d.models import ROI, CameraSettingsPatch
from camera_lab.two_d.processing import demosaic
from camera_lab.two_d.scoring import score_frame


@pytest.fixture
def light() -> SimulatedLight:
    return SimulatedLight()


@pytest.fixture
def cam(light: SimulatedLight) -> Iterator[SimulatedCamera]:
    c = SimulatedCameraDriver(light, clock=lambda: datetime(2026, 10, 21, tzinfo=UTC)).open(
        SIM_SERIAL
    )
    yield c
    c.close()


def _mean(cam: SimulatedCamera) -> float:
    cam.start_stream()
    return score_frame(cam.grab()).metric("mean_level")


# ------------------------------------------------------------- discovery
def test_discover_and_open() -> None:
    drv = SimulatedCameraDriver()
    [info] = drv.discover()
    assert (info.serial, info.vendor, info.model, info.driver) == (
        "SIM-0001",
        "camera_lab",
        "Simulated2D",
        "simulated",
    )
    with pytest.raises(CameraNotFoundError) as ei:
        drv.open("U330439")
    assert ei.value.exit_code == 3


@pytest.mark.parametrize(("w", "h"), [(8, 8), (1000, 768), (1024, 767)])
def test_bad_sensor_size(w: int, h: int) -> None:
    with pytest.raises(ValueError):
        SimulatedCameraDriver(width=w, height=h)


def test_capabilities(cam: SimulatedCamera) -> None:
    caps = cam.capabilities()
    core = {n for n in caps if "." not in n}
    assert core == {
        "exposure_us",
        "gain_db",
        "gamma",
        "wb_red",
        "wb_green",
        "wb_blue",
        "pixel_format",
        "roi_x",
        "roi_y",
        "roi_width",
        "roi_height",
        "frame_rate_hz",
        "trigger_mode",
    }
    assert {n for n in caps if "." in n} == {
        "hdr.enabled", "hdr.ch0.exposure_us", "hdr.ch1.exposure_us", "prism.exposure_mode",
        "prism.exposure_red_us", "prism.exposure_green_us", "prism.exposure_blue_us",
    }  # fmt: skip
    assert caps["pixel_format"].options == ("Mono8", "Mono12", "BayerRG8", "RGB8")
    assert caps["pixel_format"].requires_stream_stop
    assert caps["roi_width"].requires_stream_stop and caps["roi_width"].affects_calibration
    assert caps["roi_x"].affects_calibration and not caps["roi_x"].requires_stream_stop
    assert not caps["exposure_us"].affects_calibration


# --------------------------------------------------------------- pipeline
def test_open_apply_grab_score(cam: SimulatedCamera, light: SimulatedLight) -> None:
    light.set_levels({"white_ring": 80})
    res = cam.apply(CameraSettingsPatch(exposure_us=18000, gain_db=0, pixel_format="RGB8"))
    assert res.ok
    assert res.settings.exposure_us == 18000
    cam.start_stream()
    frame = cam.grab()
    assert frame.pixel_format == "RGB8" and frame.bit_depth == 8
    assert frame.data.shape == (768, 1024, 3) and frame.data.dtype == np.uint8
    assert frame.frame_id == 1
    assert frame.settings.exposure_us == 18000
    report = score_frame(frame)
    assert 0.40 < report.metric("mean_level") < 0.50
    assert report.composite > 0


@pytest.mark.parametrize(
    ("fmt", "dtype", "shape", "bits"),
    [
        ("Mono8", np.uint8, (768, 1024), 8),
        ("Mono12", np.uint16, (768, 1024), 12),
        ("BayerRG8", np.uint8, (768, 1024), 8),
        ("RGB8", np.uint8, (768, 1024, 3), 8),
    ],
)
def test_formats(
    cam: SimulatedCamera, fmt: str, dtype: type, shape: tuple[int, ...], bits: int
) -> None:
    cam.apply(CameraSettingsPatch(pixel_format=fmt, exposure_us=200000))
    cam.start_stream()
    f = cam.grab()
    assert f.data.dtype == dtype and f.data.shape == shape and f.bit_depth == bits
    assert int(f.data.max()) == (1 << bits) - 1  # long exposure saturates


def test_bayer_is_rggb(cam: SimulatedCamera, light: SimulatedLight) -> None:
    """The red colour patch must come out red after BayerRG demosaic."""
    light.set_levels({"white_ring": 80})
    cam.apply(CameraSettingsPatch(pixel_format="BayerRG8", exposure_us=20000))
    cam.start_stream()
    rgb = demosaic(cam.grab().data, "BayerRG8")
    patch = rgb[int(0.37 * 768) : int(0.47 * 768), 40:120].reshape(-1, 3).mean(axis=0)
    assert patch[0] > 2 * patch[1] and patch[0] > 2 * patch[2]


def test_frame_ids_and_determinism(light: SimulatedLight) -> None:
    a = SimulatedCameraDriver(light, seed=7).open(SIM_SERIAL)
    b = SimulatedCameraDriver(light, seed=7).open(SIM_SERIAL)
    c = SimulatedCameraDriver(light, seed=8).open(SIM_SERIAL)
    for cam in (a, b, c):
        cam.start_stream()
    fa1, fa2 = a.grab(), a.grab()
    fb1 = b.grab()
    fc1 = c.grab()
    assert (fa1.frame_id, fa2.frame_id) == (1, 2)
    assert np.array_equal(fa1.data, fb1.data)
    assert not np.array_equal(fa1.data, fa2.data)
    assert not np.array_equal(fa1.data, fc1.data)


# ------------------------------------------------------------- monotonic
def test_monotonic_in_exposure(cam: SimulatedCamera) -> None:
    means = []
    for e in (1000, 4000, 16000, 40000):
        cam.stop_stream()
        cam.apply(CameraSettingsPatch(exposure_us=e))
        means.append(_mean(cam))
    assert means == sorted(means) and len(set(means)) == 4


def test_monotonic_in_gain(cam: SimulatedCamera) -> None:
    cam.apply(CameraSettingsPatch(exposure_us=5000))
    means = []
    for g in (0, 3, 6, 12):
        cam.apply(CameraSettingsPatch(gain_db=g))
        means.append(_mean(cam))
    assert means == sorted(means) and len(set(means)) == 4


def test_monotonic_in_light(cam: SimulatedCamera, light: SimulatedLight) -> None:
    cam.apply(CameraSettingsPatch(exposure_us=10000))
    means = []
    for pct in (0, 20, 40, 80):
        light.set_levels({"white_ring": pct})
        means.append(_mean(cam))
    assert means == sorted(means) and len(set(means)) == 4


def test_no_light_no_ambient_is_dark() -> None:
    cam = SimulatedCameraDriver(None, ambient=0.0).open(SIM_SERIAL)
    cam.start_stream()
    assert score_frame(cam.grab()).metric("mean_level") < 0.01
    assert cam.light_factor() == 0.0


def test_gamma_follows_genicam(cam: SimulatedCamera) -> None:
    """X = Y^Gamma (Lucid manual, Gamma): < 1 brightens, > 1 darkens."""
    cam.apply(CameraSettingsPatch(exposure_us=5000, gamma=1.0))
    m1 = _mean(cam)
    cam.apply(CameraSettingsPatch(gamma=0.5))
    assert _mean(cam) > m1
    cam.apply(CameraSettingsPatch(gamma=2.0))
    assert _mean(cam) < m1


def test_white_balance_scales_channels(cam: SimulatedCamera, light: SimulatedLight) -> None:
    light.set_levels({"white_ring": 60})
    cam.apply(CameraSettingsPatch(pixel_format="RGB8", exposure_us=10000))
    cam.start_stream()
    base = cam.grab().data.reshape(-1, 3).mean(axis=0)
    cam.apply(CameraSettingsPatch(white_balance={"red": 2.0}))
    boosted = cam.grab().data.reshape(-1, 3).mean(axis=0)
    assert boosted[0] > 1.5 * base[0]
    assert abs(boosted[1] - base[1]) < 0.05 * base[1]


def test_defocus_reduces_sharpness(light: SimulatedLight) -> None:
    cam = SimulatedCameraDriver(light, defocus_sigma=0.0).open(SIM_SERIAL)
    cam.apply(CameraSettingsPatch(pixel_format="Mono8", exposure_us=20000))
    cam.start_stream()
    sharp = score_frame(cam.grab()).metric("sharpness")
    cam.defocus_sigma = 3.0
    assert score_frame(cam.grab()).metric("sharpness") < sharp


# ----------------------------------------------------------------- apply
def test_apply_rejects_out_of_camera_range(cam: SimulatedCamera) -> None:
    res = cam.apply(CameraSettingsPatch(gain_db=30, exposure_us=5))
    by = {r.field: r for r in res.results}
    assert by["gain_db"].status == "rejected"
    assert by["gain_db"].reason == "gain_db 30.0 > camera max 24.0"
    assert by["exposure_us"].status == "rejected"
    assert res.settings.gain_db == 0.0 and res.settings.exposure_us == 5000
    assert not res.ok


def test_apply_rounds_to_increment(cam: SimulatedCamera) -> None:
    res = cam.apply(CameraSettingsPatch(gain_db=3.04, exposure_us=1234.4))
    by = {r.field: r for r in res.results}
    assert by["gain_db"].status == "clamped" and by["gain_db"].value == pytest.approx(3.0)
    assert by["exposure_us"].status == "clamped" and by["exposure_us"].value == 1234.0
    assert res.settings.gain_db == pytest.approx(3.0)
    assert res.ok


def test_apply_white_balance_partial(cam: SimulatedCamera) -> None:
    res = cam.apply(CameraSettingsPatch(white_balance={"red": 1.5, "blue": 9.0}))
    by = {r.field: r for r in res.results}
    assert by["white_balance.red"].status == "applied"
    assert by["white_balance.blue"].status == "rejected"
    assert res.settings.white_balance == {"red": 1.5, "green": 1.0, "blue": 1.0}


def test_apply_roi(cam: SimulatedCamera) -> None:
    res = cam.apply(CameraSettingsPatch(roi=ROI(x=103, y=50, width=500, height=303)))
    [r] = res.results
    assert r.status == "clamped"
    assert res.settings.roi == ROI(x=104, y=50, width=496, height=304)
    cam.start_stream()
    assert cam.grab().data.shape == (304, 496)
    cam.stop_stream()
    bad = cam.apply(CameraSettingsPatch(roi=ROI(x=900, y=0, width=512, height=100)))
    assert bad.results[0].status == "rejected" and "exceeds sensor" in (bad.results[0].reason or "")
    bad2 = cam.apply(CameraSettingsPatch(roi=ROI(x=0, y=0, width=4, height=100)))
    assert bad2.results[0].status == "rejected"


def test_apply_pixel_format_and_trigger(cam: SimulatedCamera) -> None:
    res = cam.apply(CameraSettingsPatch(pixel_format="Mono16", trigger_mode="software"))
    by = {r.field: r for r in res.results}
    assert by["pixel_format"].status == "rejected"
    assert by["trigger_mode"].status == "applied"
    assert res.settings.trigger_mode == "software"
    assert res.settings.pixel_format == "BayerRG8"


def test_apply_frame_rate_and_gamma(cam: SimulatedCamera) -> None:
    res = cam.apply(CameraSettingsPatch(frame_rate_hz=25, gamma=0.1))
    by = {r.field: r for r in res.results}
    assert by["frame_rate_hz"].status == "applied"
    assert by["gamma"].status == "rejected"


# ----------------------------------------------------- stop-stream rule
def test_stream_stop_rule(cam: SimulatedCamera) -> None:
    cam.start_stream()
    before = cam.get_settings()
    with pytest.raises(StreamingStateError, match="pixel_format"):
        cam.apply(CameraSettingsPatch(pixel_format="Mono8", exposure_us=9999))
    with pytest.raises(StreamingStateError, match="roi size"):
        cam.apply(CameraSettingsPatch(roi=ROI(x=0, y=0, width=512, height=512)))
    assert cam.get_settings() == before  # nothing partially applied
    # same format / same size / offset-only changes are allowed while streaming
    assert cam.apply(CameraSettingsPatch(pixel_format="BayerRG8")).ok
    cam.stop_stream()
    cam.apply(CameraSettingsPatch(roi=ROI(x=0, y=0, width=512, height=512)))
    cam.start_stream()
    assert cam.apply(CameraSettingsPatch(roi=ROI(x=64, y=64, width=512, height=512))).ok
    cam.stop_stream()
    assert cam.apply(CameraSettingsPatch(pixel_format="Mono8")).ok


def test_grab_requires_stream(cam: SimulatedCamera) -> None:
    with pytest.raises(StreamingStateError, match="not streaming"):
        cam.grab()
    cam.start_stream()
    with pytest.raises(ValueError):
        cam.grab(timeout_ms=0)


# ------------------------------------------------------------- faults
def test_injected_faults(cam: SimulatedCamera) -> None:
    cam.start_stream()
    cam.inject_fault("timeout")
    with pytest.raises(CaptureTimeoutError) as ei:
        cam.grab()
    assert ei.value.exit_code == 6
    cam.inject_fault("incomplete")
    with pytest.raises(IncompleteFrameError):
        cam.grab()
    assert cam.grab().frame_id == 1  # faults consume no frame id
    with pytest.raises(ValueError):
        cam.inject_fault("smoke")


def test_closed_camera(light: SimulatedLight) -> None:
    with SimulatedCameraDriver(light).open(SIM_SERIAL) as cam:
        cam.start_stream()
        assert cam.is_streaming
    assert not cam.is_streaming
    for call in (cam.capabilities, cam.get_settings, cam.start_stream, cam.grab):
        with pytest.raises(CameraClosedError):
            call()
    with pytest.raises(CameraClosedError):
        cam.apply(CameraSettingsPatch(gain_db=1))
