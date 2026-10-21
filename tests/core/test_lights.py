"""Tests for core.lights: SimulatedLight, LightService safety, config, exit handlers."""

from __future__ import annotations

import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from typing import Any

import pytest
import yaml

from camera_lab.core.config import ConfigError
from camera_lab.core.lights import (
    DEFAULT_SIM_CHANNELS,
    LIGHT_DRIVERS,
    LightChannelConfig,
    LightService,
    SimulatedLight,
    create_light_controller,
    install_lights_off_handlers,
    load_lights_config,
)
from camera_lab.core.safety import LoadedSafetyLimits, load_safety_limits


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.t

    def sleep(self, dt: float) -> None:
        self.sleeps.append(dt)
        self.t += dt


@pytest.fixture
def limits() -> LoadedSafetyLimits:
    return load_safety_limits()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def light() -> SimulatedLight:
    return SimulatedLight()


@pytest.fixture
def svc(light: SimulatedLight, limits: LoadedSafetyLimits, clock: FakeClock) -> LightService:
    return LightService(light, limits, clock=clock, sleep=clock.sleep)


# ------------------------------------------------------------- config
def test_lights_yaml_loads_and_builds_simulated() -> None:
    cfg = load_lights_config()
    assert cfg.driver == "simulated"
    assert [c.id for c in cfg.channels] == ["white_ring", "white_bar", "red", "blue"]
    ctrl = create_light_controller(cfg)
    assert isinstance(ctrl, SimulatedLight)
    assert "simulated" in LIGHT_DRIVERS


def test_lights_yaml_matches_builtin_defaults() -> None:
    assert load_lights_config().channels == DEFAULT_SIM_CHANNELS


@pytest.mark.parametrize(
    "data",
    [
        {"driver": "simulated", "channels": []},
        {"driver": "simulated", "channels": [{"id": "a", "name": "A"}, {"id": "a", "name": "B"}]},
        {"driver": "simulated", "channels": [{"id": "Bad Id", "name": "A"}]},
        {"driver": "simulated", "channels": [{"id": "a", "name": "A", "max_pct": 150}]},
        {"driver": "Bad!", "channels": [{"id": "a", "name": "A"}]},
        {"driver": "simulated", "channels": [{"id": "a", "name": "A"}], "extra": 1},
    ],
)
def test_bad_lights_yaml(tmp_path: Path, data: dict[str, Any]) -> None:
    p = tmp_path / "lights.yaml"
    p.write_text(yaml.safe_dump(data))
    with pytest.raises(ConfigError):
        load_lights_config(p)


def test_unknown_driver(tmp_path: Path) -> None:
    p = tmp_path / "lights.yaml"
    p.write_text(yaml.safe_dump({"driver": "ledbox", "channels": [{"id": "a", "name": "A"}]}))
    with pytest.raises(KeyError, match="unknown light driver 'ledbox'"):
        create_light_controller(load_lights_config(p))


# ------------------------------------------------------ simulated light
def test_simulated_light_basics(light: SimulatedLight) -> None:
    assert [c.id for c in light.channels()] == ["white_ring", "white_bar", "red", "blue"]
    assert light.light_factor() == 0
    light.set_levels({"white_ring": 100, "red": 50})
    assert light.get_levels()["white_ring"] == 100
    assert light.light_factor() == pytest.approx(0.5 + 0.05)
    with pytest.raises(ValueError, match="unknown"):
        light.set_levels({"green": 1})
    with pytest.raises(ValueError, match="outside"):
        light.set_levels({"red": 101})
    light.close()
    assert set(light.get_levels().values()) == {0.0}
    assert light.off_count == 1


def test_simulated_light_bad_construction() -> None:
    with pytest.raises(ValueError):
        SimulatedLight([])
    a = LightChannelConfig(id="a", name="A")
    with pytest.raises(ValueError, match="duplicate"):
        SimulatedLight([a, a])


# ------------------------------------------------------- service safety
def test_ramp_0_to_80_in_25pct_steps(
    svc: LightService, light: SimulatedLight, clock: FakeClock
) -> None:
    res = svc.set_levels({"white_ring": 80})
    assert res.ok
    assert res.ramp_steps == 4
    assert [h["white_ring"] for h in light.history] == [25, 50, 75, 80]
    assert all(
        abs(b["white_ring"] - a["white_ring"]) <= 25
        for a, b in zip([{"white_ring": 0.0}, *light.history], light.history, strict=False)
    )
    assert res.levels["white_ring"] == 80
    # 2 changes/s: 2 from the burst, then 2 more at 0.5 s each
    assert clock.t == pytest.approx(1.0)


def test_ramp_down_and_multi_channel(svc: LightService, light: SimulatedLight) -> None:
    svc.set_levels({"white_ring": 60, "red": 10})
    light.history.clear()
    res = svc.set_levels({"white_ring": 0, "red": 30})
    assert res.ramp_steps == 3
    assert [(h["white_ring"], h["red"]) for h in light.history] == [(35, 30), (10, 30), (0, 30)]


def test_rejects_over_limit_with_key(svc: LightService, light: SimulatedLight) -> None:
    res = svc.set_levels({"white_ring": 90, "red": 20})
    assert not res.ok
    by = {r.channel: r for r in res.results}
    assert by["white_ring"].status == "rejected"
    assert by["white_ring"].reason == (
        "white_ring level_pct 90.0 > limit 80.0 (safety_limits.yaml: lights.default.max_pct)"
    )
    assert by["red"].status == "applied" and by["red"].value == 20
    assert light.get_levels()["white_ring"] == 0
    assert light.get_levels()["red"] == 20


@pytest.mark.parametrize("bad", [-1, float("nan"), float("inf"), "50", None, True])
def test_rejects_bad_values(svc: LightService, light: SimulatedLight, bad: Any) -> None:
    res = svc.set_levels({"red": bad})
    assert res.results[0].status == "rejected"
    assert light.history == []


def test_rejects_unknown_channel(svc: LightService) -> None:
    res = svc.set_levels({"green": 10, 5: 10})
    assert [r.status for r in res.results] == ["rejected", "rejected"]
    assert "unknown light channel" in (res.results[0].reason or "")


def test_no_change_uses_no_rate_token(svc: LightService, clock: FakeClock) -> None:
    for _ in range(5):
        res = svc.set_levels({"red": 0})
        assert res.ramp_steps == 0
    assert clock.sleeps == []


def test_rate_limit_across_calls(svc: LightService, clock: FakeClock) -> None:
    for pct in (10, 20, 30, 40):
        svc.set_levels({"red": pct})
    assert clock.t == pytest.approx(1.0)


def test_per_channel_override(tmp_path: Path, clock: FakeClock) -> None:
    data = yaml.safe_load(Path(load_safety_limits().source).read_text())
    data["lights"]["red"] = {"max_pct": 40, "max_step_pct": 10}
    p = tmp_path / "safety_limits.yaml"
    p.write_text(yaml.safe_dump(data))
    light = SimulatedLight()
    svc = LightService(light, load_safety_limits(p), clock=clock, sleep=clock.sleep)
    assert svc.set_levels({"red": 45}).results[0].reason == (
        "red level_pct 45.0 > limit 40.0 (safety_limits.yaml: lights.red.max_pct)"
    )
    assert svc.set_levels({"red": 40}).ramp_steps == 4
    assert svc.limits_for("red").max_step_pct == 10


def test_hardware_max_below_limit(limits: LoadedSafetyLimits, clock: FakeClock) -> None:
    light = SimulatedLight([LightChannelConfig(id="a", name="A", max_pct=50)])
    svc = LightService(light, limits, clock=clock, sleep=clock.sleep)
    res = svc.set_levels({"a": 60})
    assert res.results[0].reason == "a level_pct 60.0 > channel max 50.0"


def test_all_off_is_immediate(svc: LightService, light: SimulatedLight, clock: FakeClock) -> None:
    svc.set_levels({"white_ring": 80})
    t = clock.t
    svc.all_off()
    assert clock.t == t
    assert set(svc.get_levels().values()) == {0.0}


def test_context_manager_turns_off(light: SimulatedLight, limits: LoadedSafetyLimits) -> None:
    clk = FakeClock()
    with LightService(light, limits, clock=clk, sleep=clk.sleep) as svc:
        svc.set_levels({"red": 20})
        assert next(c.id for c in svc.channels()) == "white_ring"
    assert light.get_levels()["red"] == 0


# ------------------------------------------------------- exit handlers
_CHILD = textwrap.dedent(
    """
    import sys, time
    from camera_lab.core.logging import configure_logging
    from camera_lab.core.lights import SimulatedLight, install_lights_off_handlers
    configure_logging("INFO")
    light = SimulatedLight()
    install_lights_off_handlers(light)
    light.set_levels({"white_ring": 50})
    print("ready", flush=True)
    if sys.argv[1] == "exit":
        sys.exit(0)
    if sys.argv[1] == "raise":
        raise RuntimeError("boom")
    time.sleep(30)
    """
)


def _child(mode: str) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-c", _CHILD, mode],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


@pytest.mark.parametrize("mode", ["exit", "raise"])
def test_all_off_at_exit_subprocess(mode: str) -> None:
    p = _child(mode)
    out, err = p.communicate(timeout=60)
    assert "ready" in out
    assert "lights_all_off" in err
    assert "lights_all_off" not in out  # logs never on stdout


@pytest.mark.parametrize(
    ("sig", "expected_rc"), [(signal.SIGTERM, -signal.SIGTERM), (signal.SIGINT, None)]
)
def test_all_off_on_signal_subprocess(sig: signal.Signals, expected_rc: int | None) -> None:
    p = _child("sleep")
    assert p.stdout is not None
    assert p.stdout.readline().strip() == "ready"
    time.sleep(0.2)
    p.send_signal(sig)
    _, err = p.communicate(timeout=60)
    assert "lights_all_off" in err
    if expected_rc is not None:
        assert p.returncode == expected_rc
    else:
        assert p.returncode != 0
        assert "KeyboardInterrupt" in err


def test_uninstall_restores_handlers(light: SimulatedLight) -> None:
    before = signal.getsignal(signal.SIGTERM)
    uninstall = install_lights_off_handlers(light)
    assert signal.getsignal(signal.SIGTERM) is not before
    uninstall()
    assert signal.getsignal(signal.SIGTERM) is before


def test_handler_survives_failing_controller() -> None:
    code = textwrap.dedent(
        """
        from camera_lab.core.logging import configure_logging
        from camera_lab.core.lights import SimulatedLight, install_lights_off_handlers
        class Broken(SimulatedLight):
            def all_off(self):
                raise OSError("bus error")
        configure_logging("INFO")
        install_lights_off_handlers(Broken())
        """
    )
    p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert p.returncode == 0
    assert "lights_off_failed" in p.stderr
