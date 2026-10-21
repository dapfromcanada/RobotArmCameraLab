"""Bench tests on the real JAI AP-3200T-PGE (s/n U330439). Run with ``pytest -m hw``."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from camera_lab.core.lights import LightService, SimulatedLight
from camera_lab.core.safety import load_safety_limits
from camera_lab.two_d.errors import CameraBusyError, CameraNotFoundError
from camera_lab.two_d.models import ROI, CameraSettingsPatch
from camera_lab.two_d.scoring import score_frame
from camera_lab.two_d.service import TwoDService
from camera_lab.two_d.tuning import ClassicTuner

pytestmark = pytest.mark.hw
SERIAL = "U330439"
LUCID = "233100505"
FULL = ROI(x=0, y=0, width=2064, height=1544)


@pytest.fixture(scope="module")
def driver() -> Any:
    pytest.importorskip("ebus_base")
    from camera_lab.two_d.drivers.ebus import EbusDriver

    drv = EbusDriver()
    if SERIAL not in [c.serial for c in drv.discover()]:
        pytest.skip(f"AP-3200T-PGE {SERIAL} not found on the network")
    return drv


def _service(drivers: dict[str, Any]) -> TwoDService:
    limits = load_safety_limits()
    return TwoDService(limits, LightService(SimulatedLight(), limits), drivers=drivers)


@pytest.fixture
def svc(driver: Any) -> Iterator[TwoDService]:
    s = _service({"ebus": driver})
    s.open(SERIAL, "hwtest")
    s.apply(SERIAL, CameraSettingsPatch(pixel_format="RGB8", roi=FULL, gain_db=0, gamma=1.0))
    yield s
    s.shutdown()


def _mean(svc: TwoDService) -> float:
    return score_frame(svc.grab(SERIAL, timeout_ms=5000)).metric("mean_level")


def _require_light(svc: TwoDService) -> None:
    rng = svc.get_settings(SERIAL).ranges["exposure_us"]
    exp = min(100_000.0, rng.max.value)  # type: ignore[union-attr]
    svc.apply(SERIAL, CameraSettingsPatch(exposure_us=exp, gain_db=12))
    mean = _mean(svc)
    svc.apply(SERIAL, CameraSettingsPatch(exposure_us=10_000, gain_db=0))
    print(f"\n[hw] light check: mean {mean:.4f} at {exp / 1000:.1f} ms / 12 dB")
    if mean < 0.05:
        pytest.skip(f"scene too dark (mean {mean:.4f} at {exp / 1000:.1f} ms / 12 dB)")


# ------------------------------------------------------------------- tests
def test_discover_jai_only(driver: Any) -> None:
    t = time.monotonic()
    cams = driver.discover()
    dt = time.monotonic() - t
    found = [(c.serial, c.model, c.firmware) for c in cams]
    print(f"\n[hw] ebus discover: {dt * 1000:.0f} ms, {found}")
    assert SERIAL in [c.serial for c in cams]
    assert all(c.vendor == "JAI Corporation" for c in cams)
    assert dt < 5.0


def test_cross_vendor_isolation_and_discover_both_within_5s(driver: Any) -> None:
    pytest.importorskip("arena_api")
    from camera_lab.two_d.drivers.arena import ArenaDriver

    arena = ArenaDriver()
    lucids = [c.serial for c in arena.discover()]  # whichever Lucid cameras are connected
    if not lucids:
        pytest.skip(f"no Lucid camera connected (expected e.g. {LUCID})")
    with pytest.raises(CameraNotFoundError):
        arena.open(SERIAL)  # Arena never opens the JAI
    for lucid in lucids:
        with pytest.raises(CameraNotFoundError):
            driver.open(lucid)  # eBUS never opens a Lucid
    s = _service({"arena": arena, "ebus": driver})
    try:
        t = time.monotonic()
        found = {c.serial: c.driver for c in s.discover()}
        dt = time.monotonic() - t
    finally:
        s.shutdown()
    print(f"\n[hw] service discover both: {dt:.2f} s {found}")
    assert found.get(SERIAL) == "ebus"
    assert all(found.get(lucid) == "arena" for lucid in lucids)
    assert dt < 5.0


def test_set_and_read_back(svc: TwoDService) -> None:
    roi = ROI(x=512, y=400, width=1024, height=768)
    res = svc.apply(
        SERIAL,
        CameraSettingsPatch(
            exposure_us=10_000,
            gain_db=6.0,
            gamma=0.6,
            white_balance={"red": 1.5, "blue": 1.25},
            roi=roi,
        ),
    )
    print(f"\n[hw] apply: {[(r.field, r.status, r.value) for r in res.results]}")
    assert res.ok, res.results
    s = svc.get_settings(SERIAL).settings
    assert abs(s.exposure_us - 10_000) <= 1.0
    assert abs(s.gain_db - 6.0) <= 0.1
    assert s.gamma == pytest.approx(0.6, abs=0.01)
    assert s.white_balance is not None
    assert (
        abs(s.white_balance["red"] - 1.5) <= 1 / 32
        and abs(s.white_balance["blue"] - 1.25) <= 1 / 32
    )
    assert s.roi == roi
    assert svc.grab(SERIAL, timeout_ms=5000).data.shape == (768, 1024, 3)


def test_ten_consecutive_captures(svc: TwoDService) -> None:
    results = svc.capture(SERIAL, 10, note="hw ten captures")
    ids = [r.frame_id for r in results]
    print(f"\n[hw] 10 captures block ids: {ids}")
    assert len(results) == 10 and ids == sorted(ids) and len(set(ids)) == 10


@pytest.mark.parametrize("fmt", ["RGB8", "RGB10p32", "RGB12V1Packed"])
def test_full_resolution_capture_and_save_under_2s(svc: TwoDService, fmt: str) -> None:
    svc.apply(SERIAL, CameraSettingsPatch(pixel_format=fmt, exposure_us=10_000))
    svc.grab(SERIAL, timeout_ms=5000)
    t = time.monotonic()
    [cap] = svc.capture(SERIAL, 1, note=f"hw timing {fmt}")
    dt = time.monotonic() - t
    print(f"\n[hw] full-res {fmt} capture+score+save: {dt:.2f} s")
    assert cap.settings.roi == FULL and dt < 2.0


def test_lock_prevents_second_opener(svc: TwoDService, driver: Any) -> None:
    other = _service({"ebus": driver})
    with pytest.raises(CameraBusyError, match="hwtest"):
        other.open(SERIAL, "other")
    exe = Path(sys.executable).with_name("camera-lab")
    out = subprocess.run(
        [str(exe), "two-d", "capture", SERIAL, "--json"],
        capture_output=True,
        text=True,
        timeout=60,
        env=dict(os.environ),
    )
    assert out.returncode == 3 and "camera_busy" in out.stdout


def test_long_exposure_lowers_frame_rate(svc: TwoDService) -> None:
    res = svc.apply(SERIAL, CameraSettingsPatch(exposure_us=150_000))
    print(f"\n[hw] 150 ms: {res.settings.frame_rate_hz:.3f} Hz, warnings {list(res.warnings)}")
    assert res.ok and abs(res.settings.exposure_us - 150_000) <= 1
    assert svc.grab(SERIAL, timeout_ms=5000).settings.exposure_us == res.settings.exposure_us
    back = svc.apply(SERIAL, CameraSettingsPatch(exposure_us=10_000))
    assert (back.settings.frame_rate_hz or 0) > 10


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


def test_grey_world_white_balance_within_3pct(svc: TwoDService) -> None:
    _require_light(svc)
    svc.apply(
        SERIAL, CameraSettingsPatch(exposure_us=10_000, white_balance={"red": 1.0, "blue": 1.0})
    )
    result = ClassicTuner(svc, SERIAL, max_steps=20, white_balance=True).run()
    f = svc.grab(SERIAL, timeout_ms=5000)
    means = f.data.reshape(-1, 3).astype(np.float64).mean(axis=0)
    spread = (means.max() - means.min()) / means.mean()
    print(f"\n[hw] WB {result.message}; wb {f.settings.white_balance}")
    print(f"[hw] channel means {means.round(2)}; spread {spread:.3%}")
    assert spread <= 0.03
