"""Fixtures for two_d tests: a TwoDService on the simulated camera + light."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from camera_lab.core.lights import LightService, SimulatedLight
from camera_lab.core.safety import load_safety_limits
from camera_lab.two_d.drivers.simulated import SimulatedCameraDriver
from camera_lab.two_d.service import TwoDService


class FakeClock:
    """Monotonic clock whose sleep advances time instantly."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def sleep(self, dt: float) -> None:
        self.t += dt


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def sim_light() -> SimulatedLight:
    return SimulatedLight()


@pytest.fixture
def make_service(
    sim_light: SimulatedLight, clock: FakeClock, tmp_roots: Path
) -> Iterator[Callable[..., TwoDService]]:
    made: list[TwoDService] = []

    def _make(**kw: object) -> TwoDService:
        limits = load_safety_limits()
        drivers = kw.pop("drivers", None) or {"simulated": SimulatedCameraDriver(sim_light)}
        svc = TwoDService(
            limits,
            LightService(sim_light, limits, clock=clock, sleep=clock.sleep),
            drivers=drivers,  # type: ignore[arg-type]
            clock=clock,
            sleep=clock.sleep,
            calibration_root=tmp_roots / "calibration",
            **kw,  # type: ignore[arg-type]
        )
        made.append(svc)
        return svc

    yield _make
    for s in made:
        s.shutdown()


@pytest.fixture
def svc(make_service: Callable[..., TwoDService]) -> TwoDService:
    s = make_service()
    s.open("SIM-0001", "test")
    return s


def tree(root: Path) -> dict[str, int]:
    """Snapshot of every path under ``root`` with its size."""
    return {
        str(p.relative_to(root)): (p.stat().st_size if p.is_file() else -1)
        for p in sorted(root.rglob("*"))
    }
