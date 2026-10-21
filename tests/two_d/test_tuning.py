"""Tests for TuningRun and the ClassicTuner (success criterion 5)."""

from __future__ import annotations

import itertools
from collections.abc import Callable

import pytest

from camera_lab.core.lights import SimulatedLight
from camera_lab.core.records import load_run
from camera_lab.two_d.drivers.simulated import SimulatedCameraDriver
from camera_lab.two_d.errors import InvalidRequestError
from camera_lab.two_d.models import CameraSettingsPatch
from camera_lab.two_d.service import TwoDService
from camera_lab.two_d.tuning import ClassicTuner

SIM = "SIM-0001"


@pytest.mark.parametrize("pixel_format", ["BayerRG8", "Mono8"])
@pytest.mark.parametrize("start_us", [50, 500, 5000, 50000, 190000])
def test_classic_converges_from_five_starts(
    svc: TwoDService, start_us: int, pixel_format: str
) -> None:
    svc.apply(SIM, CameraSettingsPatch(exposure_us=start_us, gain_db=0, pixel_format=pixel_format))
    result = ClassicTuner(svc, SIM, max_steps=12).run()
    assert result.converged, result.history
    assert result.steps_used <= 12
    assert result.final_mean is not None and abs(result.final_mean - 0.45) <= 0.05 * 0.45
    assert result.final_clipped_pct is not None and result.final_clipped_pct <= 0.5
    rec = load_run(result.run_id)
    assert rec.method == "classic" and rec.finished_at is not None
    assert rec.best_capture_id == result.best_capture_id
    assert rec.steps_used == result.steps_used


def test_classic_run_folder_complete(svc: TwoDService) -> None:
    svc.apply(SIM, CameraSettingsPatch(exposure_us=500))
    result = ClassicTuner(svc, SIM).run()
    run = svc.get_run(result.run_id)
    steps = [s.tool for s in svc._load_recorder(run.run_id).read_steps()]
    assert steps[0] == "start_tuning_run" and steps[-1] == "finish_tuning_run"
    assert steps.count("capture") == len(result.history)
    assert result.to_dict()["converged"] is True


def test_budget_exhaustion_stops(svc: TwoDService) -> None:
    svc.apply(SIM, CameraSettingsPatch(exposure_us=50))
    result = ClassicTuner(svc, SIM, max_steps=2).run()
    assert not result.converged
    assert result.message == "step budget used up"
    assert result.steps_used == 2
    assert result.best_capture_id is not None


def test_target_mean_override(svc: TwoDService) -> None:
    result = ClassicTuner(svc, SIM, target_mean=0.3).run()
    assert result.converged and abs((result.final_mean or 0) - 0.3) <= 0.015


def test_white_balance(svc: TwoDService, sim_light: SimulatedLight) -> None:
    sim_light.set_levels({"red": 80})  # red-tinted scene
    svc.apply(SIM, CameraSettingsPatch(pixel_format="RGB8", exposure_us=20000))
    result = ClassicTuner(svc, SIM, white_balance=True).run()
    wb = svc.get_settings(SIM).settings.white_balance
    assert wb is not None and wb["red"] < 1.0
    assert "white balance" in result.message


def test_white_balance_skipped_on_mono(svc: TwoDService) -> None:
    svc.apply(SIM, CameraSettingsPatch(pixel_format="Mono8"))
    result = ClassicTuner(svc, SIM, white_balance=True).run()
    assert result.message == "converged"


def test_too_dark_raises_gain_then_needs_more_light(
    make_service: Callable[..., TwoDService],
) -> None:
    dark = SimulatedCameraDriver(None, ambient=0.02)
    svc = make_service(drivers={"simulated": dark})
    svc.open(SIM)
    result = ClassicTuner(svc, SIM, max_steps=40).run()
    gains = [h["gain_db"] for h in result.history]
    assert max(gains) > 0
    assert all(b - a <= 3.0 + 1e-9 for a, b in itertools.pairwise(gains))
    assert result.converged or "needs more light" in result.message


def test_too_bright_needs_less_light(make_service: Callable[..., TwoDService]) -> None:
    bright = SimulatedCameraDriver(
        None, ambient=40_000
    )  # too bright even at the 10 µs camera minimum
    svc = make_service(drivers={"simulated": bright})
    svc.open(SIM)
    svc.apply(SIM, CameraSettingsPatch(gain_db=6))
    result = ClassicTuner(svc, SIM, max_steps=30).run()  # 10 halvings to the 10 µs floor
    assert not result.converged
    assert "needs less light" in result.message
    assert svc.get_settings(SIM).settings.gain_db == 0


def test_never_touches_lights(svc: TwoDService, sim_light: SimulatedLight) -> None:
    sim_light.set_levels({"white_bar": 40})
    before = sim_light.get_levels()
    ClassicTuner(svc, SIM).run()
    assert sim_light.get_levels() == before


@pytest.mark.parametrize(
    ("kw"), [{"target_mean": 0.05}, {"target_mean": 0.95}, {"clip_limit_pct": 0}]
)
def test_bad_arguments(svc: TwoDService, kw: dict[str, float]) -> None:
    with pytest.raises(InvalidRequestError):
        ClassicTuner(svc, SIM, **kw)  # type: ignore[arg-type]


# ------------------------------------------------- pedestal / clip-limited scenes
@pytest.mark.parametrize("start_us", [50, 500, 5000, 50000, 190000])
def test_converges_with_black_level_pedestal(
    make_service: Callable[..., TwoDService], sim_light: SimulatedLight, start_us: int
) -> None:
    """A JAI-like ~3 % pedestal must not slow convergence (linear secant)."""
    drv = SimulatedCameraDriver(sim_light, black_level=0.033)
    svc = make_service(drivers={"simulated": drv})
    svc.open(SIM)
    svc.apply(SIM, CameraSettingsPatch(exposure_us=start_us))
    result = ClassicTuner(svc, SIM, max_steps=12).run()
    assert result.converged, result.history
    assert result.stop_reason == "converged" and result.steps_used <= 12


def test_clip_limited_scene_stops_early(
    make_service: Callable[..., TwoDService], sim_light: SimulatedLight
) -> None:
    """A small highlight that clips first: stop clip-limited instead of burning the budget."""
    drv = SimulatedCameraDriver(sim_light, hot_spot_pct=1.0)
    svc = make_service(drivers={"simulated": drv})
    svc.open(SIM)
    svc.apply(SIM, CameraSettingsPatch(exposure_us=1000, pixel_format="RGB8"))
    result = ClassicTuner(svc, SIM, max_steps=30, white_balance=True).run()
    assert result.stop_reason == "clip_limited", result.history
    assert not result.converged and result.steps_used < 30
    assert "clip-limited" in result.message and "white balance applied" in result.message
    assert result.final_clipped_pct is not None and result.final_clipped_pct <= 0.5
    assert result.final_mean is not None and result.final_mean < 0.45
    assert result.to_dict()["stop_reason"] == "clip_limited"


def test_white_balance_with_pedestal_within_3pct(
    make_service: Callable[..., TwoDService], sim_light: SimulatedLight
) -> None:
    """Grey world must work above the pedestal (as on the JAI), not on raw means."""
    sim_light.set_levels({"red": 80, "white_ring": 40})  # warm light
    drv = SimulatedCameraDriver(sim_light, black_level=0.04)
    svc = make_service(drivers={"simulated": drv})
    svc.open(SIM)
    svc.apply(SIM, CameraSettingsPatch(pixel_format="RGB8", exposure_us=10_000))
    result = ClassicTuner(svc, SIM, max_steps=20, white_balance=True).run()
    frame = svc.grab(SIM)
    signal = frame.data.reshape(-1, 3).astype(float).mean(axis=0) - 0.04 * 255
    spread = (signal.max() - signal.min()) / signal.mean()
    assert spread <= 0.03, (signal, result.message)
    assert "pedestal" in result.history[-1]


def test_never_aims_past_a_clipping_bound(svc: TwoDService) -> None:
    """Regression (JAI bench): with a clipped upper bound, the secant must not jump back to it."""
    tuner = ClassicTuner(svc, SIM)
    pts = [(13_439.0, 0.069), (44_451.0, 0.158)]
    guess = tuner._next_exposure(44_451.0, 0.158, 0.35, pts, 44_451.0, 147_027.0, hi_clipped=True)
    assert guess == pytest.approx((44_451.0 * 147_027.0) ** 0.5)
    free = tuner._next_exposure(44_451.0, 0.158, 0.35, pts, 44_451.0, 147_027.0, hi_clipped=False)
    assert free > guess  # without clipping the secant may go further
