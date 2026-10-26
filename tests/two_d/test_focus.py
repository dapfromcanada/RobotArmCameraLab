"""The focus meter (Derek 2026-10-26: a focus tool to see what is happening)."""

from __future__ import annotations

from dataclasses import replace

import cv2
import numpy as np
import pytest

from camera_lab.two_d.focus import FocusMeter, FocusReading, next_exposure
from camera_lab.two_d.service import TwoDService

from .scene import Scene

SIM = "SIM-0001"


@pytest.fixture
def scene(svc: TwoDService, monkeypatch: pytest.MonkeyPatch) -> Scene:
    sc = Scene(svc)
    monkeypatch.setattr(svc, "grab", sc.grab)
    return sc


def _read(svc: TwoDService, meter: FocusMeter) -> FocusReading:
    return meter.read(svc.grab(SIM), float(svc.get_settings(SIM).settings.exposure_us))


def test_sharpness_per_plane_and_best(svc: TwoDService, scene: Scene) -> None:
    meter = FocusMeter()
    r = _read(svc, meter)
    assert r.found and set(r.mtf50) == {"R", "G", "B"}
    assert r.main is not None and 0.05 < r.main < 0.5
    assert r.edges == r.edges_total == 8
    scene.page = cv2.GaussianBlur(scene.page, (0, 0), 2.0)  # the ring turned off focus
    soft = _read(svc, meter)
    assert soft.main is not None and soft.main < r.main
    assert soft.best_main == r.main
    assert "past the sharpest point" in soft.hint


def test_moving_the_chart_starts_the_best_again(svc: TwoDService, scene: Scene) -> None:
    meter = FocusMeter()
    assert _read(svc, meter).best_main is not None
    scene.page = np.roll(scene.page, 200, axis=1)  # the chart moved sideways
    moved = _read(svc, meter)
    assert moved.reset and moved.best_main == moved.main


def test_warnings_say_what_spoils_the_reading(svc: TwoDService, scene: Scene) -> None:
    meter = FocusMeter()
    _read(svc, meter)
    scene.k /= 4  # the light dropped (iris closed or floods turned away)
    dim = _read(svc, meter)
    assert any("the light is 0.2" in w for w in dim.warnings)
    scene.page = cv2.resize(scene.page, None, fx=0.3, fy=0.3)  # the chart far away
    far = FocusMeter().read(svc.grab(SIM), 5000.0)
    assert not far.found or any("too small" in w for w in far.warnings)
    scene.page = np.full_like(scene.page, 0.9)  # no chart at all
    none = FocusMeter().read(svc.grab(SIM), 5000.0)
    assert not none.found and "resolution chart" in none.warnings[-1]


def test_exposure_follows_the_light(svc: TwoDService, scene: Scene) -> None:
    r = _read(svc, FocusMeter())
    dark = replace(r, highlights=0.2)
    assert next_exposure(dark, 10.0, 100_000.0) == pytest.approx(r.exposure_us * 0.7 / 0.2)
    hot = replace(r, highlights=0.99, clipped_pct=3.0)
    assert next_exposure(hot, 10.0, 100_000.0) == pytest.approx(r.exposure_us * 0.5)
    ok = replace(r, highlights=0.7, clipped_pct=0.0)
    assert next_exposure(ok, 10.0, 100_000.0) is None
