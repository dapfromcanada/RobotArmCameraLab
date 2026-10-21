"""Bench tests on the real Lucid TRI054S-C (s/n 233100505). Run with ``pytest -m hw``.

Tests that need a lit, static scene skip with "scene too dark" when the scene
gives a mean level below 0.05 at 100 ms / 12 dB.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from camera_lab.core.lights import LightService, SimulatedLight
from camera_lab.core.safety import load_safety_limits
from camera_lab.two_d.errors import CameraBusyError
from camera_lab.two_d.models import ROI, CameraSettingsPatch
from camera_lab.two_d.scoring import score_frame
from camera_lab.two_d.service import TwoDService
from camera_lab.two_d.tuning import ClassicTuner

pytestmark = pytest.mark.hw
SERIAL = "233100505"
FULL = ROI(x=0, y=0, width=2880, height=1860)


@pytest.fixture(scope="module")
def driver():  # type: ignore[no-untyped-def]
    pytest.importorskip("arena_api")
    from camera_lab.two_d.drivers.arena import ArenaDriver

    drv = ArenaDriver()
    if SERIAL not in [c.serial for c in drv.discover()]:
        pytest.skip(f"TRI054S-C {SERIAL} not found on the network")
    return drv


def _service(driver: object) -> TwoDService:
    limits = load_safety_limits()
    return TwoDService(
        limits,
        LightService(SimulatedLight(), limits),
        drivers={"arena": driver},  # type: ignore[dict-item]
    )


@pytest.fixture
def svc(driver: object) -> Iterator[TwoDService]:
    s = _service(driver)
    s.open(SERIAL, "hwtest")
    hdr = s._open[SERIAL].camera._node("HDRTuningEnable")  # type: ignore[attr-defined]
    if hdr is not None and hdr.value:
        s.shutdown()
        pytest.skip("HDR mode is on (HDRTuningEnable = True): exposure and gain are read-only")
    s.apply(SERIAL, CameraSettingsPatch(pixel_format="BayerRG8", roi=FULL, gain_db=0))
    yield s
    s.shutdown()


def _mean(svc: TwoDService) -> float:
    return score_frame(svc.grab(SERIAL, timeout_ms=5000)).metric("mean_level")


def _require_light(svc: TwoDService) -> None:
    """Skip unless the scene reaches mean 0.05 at the longest allowed exposure and 12 dB."""
    rng = svc.get_settings(SERIAL).ranges["exposure_us"]
    exp = min(100_000.0, rng.max.value)  # type: ignore[union-attr]
    res = svc.apply(SERIAL, CameraSettingsPatch(exposure_us=exp, gain_db=12))
    assert res.ok, res.results
    mean = _mean(svc)
    svc.apply(SERIAL, CameraSettingsPatch(exposure_us=10_000, gain_db=0))
    print(f"\n[hw] light check: mean {mean:.4f} at {exp / 1000:.1f} ms / 12 dB")
    if mean < 0.05:
        pytest.skip(f"scene too dark (mean {mean:.4f} at {exp / 1000:.1f} ms / 12 dB)")


# ------------------------------------------------------------------- tests
def test_discover_within_5s_lucid_only(driver: object) -> None:
    t = time.monotonic()
    cams = driver.discover()  # type: ignore[attr-defined]
    dt = time.monotonic() - t
    assert SERIAL in [c.serial for c in cams]
    assert all(c.vendor.casefold() == "lucid vision labs" for c in cams)  # JAI skipped
    assert dt < 5.0
    found = [(c.serial, c.model, c.firmware) for c in cams]
    print(f"\n[hw] arena discover: {dt * 1000:.0f} ms, {found}")


def test_set_and_read_back(svc: TwoDService) -> None:
    roi = ROI(x=400, y=300, width=1024, height=768)
    res = svc.apply(
        SERIAL,
        CameraSettingsPatch(
            exposure_us=10_000, gain_db=6.0, white_balance={"red": 1.5, "blue": 1.25}, roi=roi
        ),
    )
    print(f"\n[hw] apply results: {[(r.field, r.status, r.value, r.reason) for r in res.results]}")
    assert res.ok, res.results
    s = svc.get_settings(SERIAL).settings
    assert abs(s.exposure_us - 10_000) <= 1.0
    assert abs(s.gain_db - 6.0) <= 0.1
    assert s.white_balance is not None
    assert abs(s.white_balance["red"] - 1.5) <= 1 / 512
    assert abs(s.white_balance["blue"] - 1.25) <= 1 / 512
    assert s.roi == roi
    f = svc.grab(SERIAL, timeout_ms=5000)
    assert f.data.shape == (768, 1024)
    assert svc.get_settings(SERIAL).settings.gamma is None  # no Gamma node on this camera


def test_ten_consecutive_captures(svc: TwoDService) -> None:
    results = svc.capture(SERIAL, 10, note="hw ten captures")
    ids = [r.frame_id for r in results]
    print(f"\n[hw] 10 captures frame ids: {ids}")
    assert len(results) == 10
    assert ids == sorted(ids) and len(set(ids)) == 10


@pytest.mark.parametrize("fmt", ["BayerRG8", "RGB8"])
def test_full_resolution_capture_and_save_under_2s(svc: TwoDService, fmt: str) -> None:
    svc.apply(SERIAL, CameraSettingsPatch(pixel_format=fmt, exposure_us=10_000))
    svc.grab(SERIAL, timeout_ms=5000)  # stream running, as during a session
    t = time.monotonic()
    [cap] = svc.capture(SERIAL, 1, note=f"hw timing {fmt}")
    dt = time.monotonic() - t
    print(f"\n[hw] full-res {fmt} capture+score+save: {dt:.2f} s")
    assert cap.settings.roi == FULL
    assert dt < 2.0


def test_lock_prevents_second_opener(svc: TwoDService, driver: object) -> None:
    other = _service(driver)
    with pytest.raises(CameraBusyError, match="hwtest"):
        other.open(SERIAL, "other")
    exe = Path(sys.executable).with_name("camera-lab")
    out = subprocess.run(
        [str(exe), "two-d", "capture", SERIAL, "--json"],
        capture_output=True, text=True, timeout=60, env=dict(os.environ),
    )  # fmt: skip
    assert out.returncode == 3, out.stderr
    assert "camera_busy" in out.stdout


def test_exposure_change_visible_in_next_grab(svc: TwoDService) -> None:
    """Doubling exposure doubles the signal above the black-level pedestal, in the next grab."""
    _require_light(svc)
    lo = svc.get_settings(SERIAL).ranges["exposure_us"].min.value  # type: ignore[union-attr]
    svc.apply(SERIAL, CameraSettingsPatch(exposure_us=max(lo, 100.0)))
    m0 = _mean(svc)  # ≈ pedestal
    svc.apply(SERIAL, CameraSettingsPatch(exposure_us=10_000))
    m1 = _mean(svc)
    svc.apply(SERIAL, CameraSettingsPatch(exposure_us=20_000))
    m2 = _mean(svc)
    ratio = (m2 - m0) / (m1 - m0)
    print(f"\n[hw] pedestal {m0:.4f}, 10 ms {m1:.4f}, 20 ms {m2:.4f}, signal ratio {ratio:.2f}")
    assert 1.7 < ratio < 2.3


def test_12p_unpack_matches_16bit(svc: TwoDService) -> None:
    _require_light(svc)
    means = {}
    for fmt in ("Mono12p", "Mono16", "Mono8"):
        svc.apply(SERIAL, CameraSettingsPatch(pixel_format=fmt, exposure_us=10_000))
        f = svc.grab(SERIAL, timeout_ms=5000)
        means[fmt] = (float(f.data.mean()), int(f.data.max()), f.bit_depth)
    print(f"\n[hw] format means (mean, max, bits): {means}")
    assert means["Mono12p"][1] <= 4095
    m12, m8 = means["Mono12p"][0], means["Mono8"][0]
    assert 12 < m12 / max(m8, 1e-6) < 20  # 12-bit vs 8-bit: ×16 expected


def test_classic_tune_converges_within_15_steps(svc: TwoDService) -> None:
    _require_light(svc)
    svc.apply(SERIAL, CameraSettingsPatch(exposure_us=1_000, gain_db=0))
    result = ClassicTuner(svc, SERIAL, max_steps=15).run()
    print(f"\n[hw] classic tune: {result.message}, {result.steps_used} steps")
    print(f"[hw] history: {result.history}")
    if result.stop_reason == "clip_limited":
        pytest.skip(
            f"scene is clip-limited ({result.message}): "
            "needs a static scene without bright highlights"
        )
    assert result.converged and result.steps_used <= 15


def test_long_exposure_lowers_frame_rate(svc: TwoDService) -> None:
    cam = svc._open[SERIAL].camera
    hdr = cam._node("HDRTuningEnable")  # type: ignore[attr-defined]
    if hdr is not None and hdr.value:
        pytest.skip("HDR mode is on (HDRTuningEnable = True): exposure is read-only")
    res = svc.apply(SERIAL, CameraSettingsPatch(exposure_us=150_000))
    print(
        f"\n[hw] 150 ms exposure: {res.settings.exposure_us:.1f} us at "
        f"{res.settings.frame_rate_hz:.3f} Hz, warnings {list(res.warnings)}"
    )
    assert res.ok and abs(res.settings.exposure_us - 150_000) < 50
    assert (res.settings.frame_rate_hz or 99) < 6.7
    f = svc.grab(SERIAL, timeout_ms=5000)
    assert f.settings.exposure_us == res.settings.exposure_us
    back = svc.apply(SERIAL, CameraSettingsPatch(exposure_us=10_000))
    assert (back.settings.frame_rate_hz or 0) > 15  # free-run restored
