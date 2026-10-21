"""Phase 6b bench tests: profile modes and strategies on the real cameras (``pytest -m hw``).

The Triton tests change its HDR settings, so they run only with
``CAMERA_LAB_HW_TRITON_MODES=1`` (set it when the Triton is free to be changed).
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import numpy as np
import pytest

from camera_lab.core.lights import LightService, SimulatedLight
from camera_lab.core.safety import load_safety_limits
from camera_lab.two_d.camera_config import load_cameras_config
from camera_lab.two_d.service import TwoDService

pytestmark = pytest.mark.hw


def _service(drivers: dict[str, Any]) -> TwoDService:
    limits = load_safety_limits()
    return TwoDService(
        limits,
        LightService(SimulatedLight(), limits),
        drivers=drivers,
        cameras=load_cameras_config(),
    )


@pytest.fixture(scope="module")
def ebus() -> Any:
    pytest.importorskip("ebus_base")
    from camera_lab.two_d.drivers.ebus import EbusDriver

    drv = EbusDriver()
    if "U330439" not in [c.serial for c in drv.discover()]:
        pytest.skip("AP-3200T-PGE not found")
    return drv


@pytest.fixture
def jai(ebus: Any) -> Iterator[TwoDService]:
    s = _service({"ebus": ebus})
    s.open("apex", "hwtest")
    yield s
    s.shutdown()


@pytest.mark.parametrize("wb_method", ["gain", "exposure"])
def test_jai_prism_mode_both_wb_methods(jai: TwoDService, wb_method: str) -> None:
    r = jai.tune("apex", white_balance=True, max_steps=25, options={"wb_method": wb_method})
    f = jai.grab("U330439", timeout_ms=5000)
    s = f.settings
    means = f.data.reshape(-1, 3).astype(np.float64).mean(axis=0)
    spread = (means.max() - means.min()) / means.mean()
    exps = [s.advanced.get(f"prism.exposure_{c}_us") for c in ("red", "green", "blue")]
    print(
        f"\n[hw] JAI prism wb={wb_method}: {r.stop_reason}, {r.steps_used} steps; gains "
        f"{s.white_balance}; exposure mode {s.advanced.get('prism.exposure_mode')} R/G/B "
        f"{exps}; fps {s.frame_rate_hz:.2f}; means {means.round(1)} spread {spread:.2%}"
    )
    assert (r.mode, r.strategy) == ("prism", "prism")
    assert r.stop_reason in ("converged", "clip_limited"), r.message
    assert spread <= 0.03
    if wb_method == "exposure":
        assert s.advanced["prism.exposure_mode"] == "individual"
        assert s.white_balance == {"red": 1.0, "green": 1.0, "blue": 1.0}


@pytest.fixture
def triton() -> Iterator[TwoDService]:
    if os.environ.get("CAMERA_LAB_HW_TRITON_MODES") != "1":
        pytest.skip(
            "Triton mode tests change HDR settings: set CAMERA_LAB_HW_TRITON_MODES=1 when free"
        )
    pytest.importorskip("arena_api")
    from camera_lab.two_d.drivers.arena import ArenaDriver

    s = _service({"arena": ArenaDriver()})
    s.open("triton", "hwtest")
    yield s
    s.shutdown()


@pytest.mark.parametrize("mode", ["hdr", "linear"])
def test_triton_modes(triton: TwoDService, mode: str) -> None:
    res = triton.set_mode("triton", mode)
    adv = res.settings.advanced
    print(
        f"\n[hw] Triton mode {mode}: hdr.enabled {adv.get('hdr.enabled')}, "
        f"output {adv.get('hdr.output')}, "
        f"channels {[adv.get(f'hdr.ch{i}.exposure_us') for i in range(4)]}"
    )
    assert adv["hdr.enabled"] == ("on" if mode == "hdr" else "off")
    r = triton.tune("triton", mode, max_steps=15)
    print(
        f"[hw] Triton tune {mode}: {r.stop_reason} — {r.message}, {r.steps_used} steps, "
        f"mean {r.final_mean}, clipped {r.final_clipped_pct}"
    )
    assert r.strategy == ("hdr" if mode == "hdr" else "linear")
    assert r.stop_reason in ("converged", "clip_limited", "too_dark"), r.message
