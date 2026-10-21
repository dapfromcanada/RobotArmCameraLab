"""Bench tests on the real Lucid TRI089S-M (s/n 244500230, mono). Run with ``pytest -m hw``.

Facts checked here were read from the live node map on 2026-10-21
(docs/cameras/TRI089S-M_244500230_fw1.97.0.0_nodemap.tsv). Tests that need
light skip with "scene too dark" below a mean level of 0.02 at 100 ms / 12 dB.
"""

from __future__ import annotations

import time
from collections.abc import Iterator

import numpy as np
import pytest

from camera_lab.core.lights import LightService, SimulatedLight
from camera_lab.core.safety import load_safety_limits
from camera_lab.two_d.models import ROI, CameraSettingsPatch
from camera_lab.two_d.scoring import score_frame
from camera_lab.two_d.service import TwoDService

pytestmark = pytest.mark.hw
SERIAL = "244500230"
FULL = ROI(x=0, y=0, width=4096, height=2160)
MIN_LIVE_FPS = 5.0  # success criterion 7 (Derek, 2026-10-21)


@pytest.fixture(scope="module")
def driver():  # type: ignore[no-untyped-def]
    pytest.importorskip("arena_api")
    from camera_lab.two_d.drivers.arena import ArenaDriver

    drv = ArenaDriver()
    if SERIAL not in [c.serial for c in drv.discover()]:
        pytest.skip(f"TRI089S-M {SERIAL} not found on the network")
    return drv


@pytest.fixture
def svc(driver: object) -> Iterator[TwoDService]:
    limits = load_safety_limits()
    s = TwoDService(
        limits,
        LightService(SimulatedLight(), limits),
        drivers={"arena": driver},  # type: ignore[dict-item]
    )
    s.open(SERIAL, "hwtest")
    s.apply(SERIAL, CameraSettingsPatch(pixel_format="Mono8", roi=FULL, gain_db=0, gamma=1.0))
    try:
        yield s
        s.apply(SERIAL, CameraSettingsPatch(pixel_format="Mono8", exposure_us=10_000, gain_db=0))
    finally:
        s.shutdown()


def _mean(svc: TwoDService) -> float:
    return score_frame(svc.grab(SERIAL, timeout_ms=5000)).metric("mean_level")


def test_opens_as_mono_without_white_balance(svc: TwoDService) -> None:
    view = svc.get_settings(SERIAL)
    s = view.settings
    print(f"\n[hw] TRI089S-M settings: {s.model_dump(exclude={'advanced'})}")
    assert s.white_balance is None
    assert not any(k.startswith("wb_") for k in view.ranges)
    assert s.gamma == pytest.approx(1.0)  # this camera has Gamma (the TRI054S does not)
    assert s.roi == FULL
    assert svc.current_mode(SERIAL) in (None, "linear")
    assert svc.get_profile(SERIAL).default_mode == "linear"


@pytest.mark.parametrize(
    ("fmt", "dtype", "top"), [("Mono8", np.uint8, 255), ("Mono12p", np.uint16, 4095)]
)
def test_formats_and_free_run_rate(svc: TwoDService, fmt: str, dtype: type, top: int) -> None:
    svc.apply(SERIAL, CameraSettingsPatch(pixel_format=fmt, exposure_us=10_000))
    f = svc.grab(SERIAL, timeout_ms=5000)
    t, n = time.monotonic(), 0
    while time.monotonic() - t < 3.0:
        svc.grab(SERIAL, timeout_ms=5000)
        n += 1
    fps = n / (time.monotonic() - t)
    print(
        f"\n[hw] {fmt}: {f.data.shape} {f.data.dtype}, grabbed {fps:.1f} fps, "
        f"camera {f.settings.frame_rate_hz:.2f}"
    )
    assert f.data.shape == (2160, 4096) and f.data.dtype == dtype
    assert int(f.data.max()) <= top
    assert fps >= MIN_LIVE_FPS


def test_long_exposure_lowers_then_restores_frame_rate_while_streaming(svc: TwoDService) -> None:
    svc.grab(SERIAL, timeout_ms=5000)  # streaming, as during a session
    free = svc.get_settings(SERIAL).settings.frame_rate_hz
    long = svc.apply(SERIAL, CameraSettingsPatch(exposure_us=150_000))
    f = svc.grab(SERIAL, timeout_ms=5000)
    back = svc.apply(SERIAL, CameraSettingsPatch(exposure_us=10_000))
    print(
        f"\n[hw] free {free:.2f} fps → {long.settings.frame_rate_hz:.2f} fps at 150 ms "
        f"→ {back.settings.frame_rate_hz:.2f}"
    )
    assert long.ok and f.settings.exposure_us == pytest.approx(150_000, rel=1e-3)
    assert long.settings.frame_rate_hz < free
    assert any("restored" in w for w in back.warnings)
    assert back.settings.frame_rate_hz == pytest.approx(free, rel=1e-3)


def test_set_and_read_back_gain_gamma(svc: TwoDService) -> None:
    res = svc.apply(SERIAL, CameraSettingsPatch(exposure_us=12_345, gain_db=6.0, gamma=0.8))
    s = res.settings
    print(f"\n[hw] read back: exposure {s.exposure_us}, gain {s.gain_db}, gamma {s.gamma}")
    assert res.ok, res.results
    assert s.exposure_us == pytest.approx(12_345, rel=1e-3)
    assert s.gain_db == pytest.approx(6.0, abs=0.1)
    assert s.gamma == pytest.approx(0.8, abs=0.01)
    svc.apply(SERIAL, CameraSettingsPatch(gamma=1.0, gain_db=0))


def test_exposure_change_visible_in_next_grab(svc: TwoDService) -> None:
    svc.apply(SERIAL, CameraSettingsPatch(exposure_us=100_000, gain_db=12))
    if _mean(svc) < 0.02:
        pytest.skip("scene too dark")
    svc.apply(SERIAL, CameraSettingsPatch(exposure_us=10_000))
    lo = _mean(svc)
    svc.apply(SERIAL, CameraSettingsPatch(exposure_us=20_000))
    hi = _mean(svc)
    print(f"\n[hw] mean 10 ms {lo:.4f} → 20 ms {hi:.4f} (ratio {hi / lo:.2f})")
    assert 1.5 < hi / lo < 2.5


def test_full_resolution_capture_and_save_under_2s(svc: TwoDService) -> None:
    svc.apply(SERIAL, CameraSettingsPatch(exposure_us=10_000))
    svc.grab(SERIAL, timeout_ms=5000)
    t = time.monotonic()
    [cap] = svc.capture(SERIAL, 1, note="hw timing Mono8")
    dt = time.monotonic() - t
    print(f"\n[hw] full-res Mono8 capture+score+save: {dt:.2f} s")
    assert cap.settings.roi == FULL
    assert dt < 2.0


def test_linear_tune_from_profile(svc: TwoDService) -> None:
    r = svc.tune(SERIAL, max_steps=15)
    print(
        f"\n[hw] tune: {r.mode}/{r.strategy} {r.stop_reason} — {r.message}, "
        f"{r.steps_used} steps, mean {r.final_mean}"
    )
    assert (r.mode, r.strategy) == ("linear", "linear")
    assert r.stop_reason in ("converged", "clip_limited", "too_dark"), r.message
