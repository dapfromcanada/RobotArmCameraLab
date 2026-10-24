"""Phase 6b: camera profiles, advanced settings, modes and tuning strategies."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import yaml
from pydantic import ValidationError
from typer.testing import CliRunner

from camera_lab.cli import app
from camera_lab.core.config import ConfigError
from camera_lab.core.lights import LightService, SimulatedLight
from camera_lab.core.safety import load_safety_limits
from camera_lab.two_d.drivers.arena import ArenaDriver
from camera_lab.two_d.drivers.ebus import EbusDriver
from camera_lab.two_d.errors import (
    ConfigurationError,
    InvalidRequestError,
    LimitRejectedError,
    NotFoundError,
)
from camera_lab.two_d.models import CameraSettingsPatch
from camera_lab.two_d.profiles import CameraProfile, generic_profile, load_profile, profiles_dir
from camera_lab.two_d.service import TwoDService
from camera_lab.two_d.strategies import STRATEGIES

from .fake_arena import FakeSystem
from .fake_ebus import FakeSdk

SIM = "SIM-0001"
runner = CliRunner()


def _by(res: Any) -> dict[str, Any]:
    return {r.field: r for r in res.results}


# --------------------------------------------------------------- profiles
def test_shipped_profiles_load() -> None:
    triton = load_profile("233100505")
    assert triton.default_mode == "raw_hdr" and set(triton.modes) == {"raw_hdr", "hdr", "linear"}
    assert triton.characteristics.shutter == "rolling" and triton.characteristics.hdr_capable
    jai = load_profile("U330439")
    assert jai.characteristics.colour == "prism" and jai.characteristics.shutter == "global"
    assert jai.characteristics.black_level == pytest.approx(0.031)
    assert jai.modes["prism"].options["wb_method"] == "gain"
    sim = load_profile(SIM)
    assert set(sim.modes) == {"linear", "hdr", "prism"}
    for p in (triton, jai, sim):
        for m in p.modes.values():
            assert m.strategy in STRATEGIES


def test_generic_profile_for_unknown_camera() -> None:
    p = load_profile("ABC-123")
    assert p == generic_profile("ABC-123")
    assert p.mode()[0] == "linear"


@pytest.mark.parametrize(
    "data",
    [
        {"serial": "X1", "default_mode": "a", "modes": {}},
        {
            "serial": "X1",
            "default_mode": "b",
            "modes": {"a": {"strategy": "linear", "knobs": ["gain_db"]}},
        },
        {
            "serial": "X1",
            "default_mode": "a",
            "modes": {"a": {"strategy": "linear", "knobs": ["nonsense"]}},
        },
        {
            "serial": "X1",
            "default_mode": "A B",
            "modes": {"A B": {"strategy": "linear", "knobs": ["gain_db"]}},
        },
        {"serial": "X1", "default_mode": "a", "modes": {"a": {"strategy": "linear", "knobs": []}}},
        {
            "serial": "X1",
            "default_mode": "a",
            "extra": 1,
            "modes": {"a": {"strategy": "linear", "knobs": ["gain_db"]}},
        },
    ],
)
def test_invalid_profiles(tmp_path: Path, data: dict[str, Any]) -> None:
    (tmp_path / "X1.yaml").write_text(yaml.safe_dump(data))
    with pytest.raises(ConfigError):
        load_profile("X1", tmp_path)


def test_profile_serial_must_match_file(tmp_path: Path) -> None:
    data = yaml.safe_load((profiles_dir() / "SIM-0001.yaml").read_text())
    (tmp_path / "X2.yaml").write_text(yaml.safe_dump(data))
    with pytest.raises(ConfigError, match="does not match"):
        load_profile("X2", tmp_path)


def test_unknown_mode() -> None:
    with pytest.raises(KeyError):
        load_profile(SIM).mode("nope")


def test_profile_model_roundtrip() -> None:
    p = load_profile("233100505")
    assert CameraProfile.model_validate(p.model_dump()) == p


# ------------------------------------------------------- advanced limits
def test_working_range_by_name_wildcard_and_unit() -> None:
    """Working ranges (guidance) replace the old advanced safety limits (limits policy)."""
    prof = CameraProfile.model_validate(
        {
            "serial": SIM,
            "default_mode": "linear",
            "modes": {"linear": {"strategy": "linear", "knobs": ["exposure_us"]}},
            "working_range": {
                "exposure_us": {"max": 50_000},
                "gain_db": {"min": 0, "max": 12},
                "hdr.ch*.gain_db": {"max": 30},
                "hdr.ch0.exposure_us": {"min": 100, "max": 1000},
            },
        }
    )
    assert prof.working("hdr.ch0.exposure_us").max == 1000  # exact name
    assert prof.working("hdr.ch2.gain_db").max == 30  # wildcard
    assert prof.working("hdr.ch1.exposure_us").max == 50_000  # by unit (_us → exposure_us)
    assert prof.working("prism.exposure_red_us").max == 50_000
    assert prof.working("gamma").max is None  # no guidance
    assert generic_profile(SIM).working("gain_db").max == 18  # default: the old caps, as guidance
    with pytest.raises(ValidationError):
        CameraProfile.model_validate(
            {
                "serial": SIM,
                "default_mode": "m",
                "modes": {"m": {"strategy": "linear", "knobs": ["exposure_us"]}},
                "working_range": {"gain_db": {"min": 10, "max": 5}},
            }
        )


def test_old_advanced_limits_are_refused(tmp_path: Path) -> None:
    data = yaml.safe_load(Path(load_safety_limits().source).read_text())
    data["cameras"]["default"]["advanced"] = {"Bad Name": {"min": 0, "max": 1}}
    p = tmp_path / "safety_limits.yaml"
    p.write_text(yaml.safe_dump(data))
    from camera_lab.core.safety import SafetyConfigError

    with pytest.raises(SafetyConfigError):
        load_safety_limits(p)


def test_advanced_patch_names_validated() -> None:
    with pytest.raises(ValidationError):
        CameraSettingsPatch(advanced={"NoDot": 1})


# --------------------------------------------------- service: advanced
def test_service_applies_advanced_with_safety(svc: TwoDService) -> None:
    res = svc.apply(
        SIM,
        CameraSettingsPatch(
            advanced={
                "hdr.ch0.exposure_us": 2_000_000,  # > camera max 1e6 (no camera_lab cap)
                "hdr.ch1.exposure_us": 1234.4,  # rounded to the camera increment
                "hdr.enabled": "maybe",  # not an option
                "vendor.unknown": 1,  # not supported
            }
        ),
    )
    by = _by(res)
    assert by["advanced.hdr.ch0.exposure_us"].status == "rejected"
    assert "camera max 1000000.0" in by["advanced.hdr.ch0.exposure_us"].reason
    assert by["advanced.hdr.ch1.exposure_us"].status == "clamped"
    assert res.settings.advanced["hdr.ch1.exposure_us"] == 1234.0
    assert by["advanced.hdr.enabled"].status == "rejected"
    assert "not supported" in by["advanced.vendor.unknown"].reason
    view = svc.get_settings(SIM)
    assert view.ranges["hdr.ch0.exposure_us"].max.value == 1_000_000  # type: ignore[union-attr]
    assert view.working["hdr.ch0.exposure_us"]["max"] == 200_000  # guidance, by unit
    assert view.options["hdr.enabled"] == ("off", "on")


def test_advanced_logged_in_run(svc: TwoDService) -> None:
    run = svc.start_run(SIM, "g", 5)
    svc.apply(SIM, CameraSettingsPatch(advanced={"hdr.enabled": "on"}), "agent")
    step = run.recorder.read_steps()[-1]
    assert step.arguments == {"advanced": {"hdr.enabled": "on"}}


# --------------------------------------------------------- modes + tune
def test_set_mode_applies_settings_and_scoring(svc: TwoDService) -> None:
    assert svc.current_mode(SIM) == "linear"  # recognised from the camera's settings
    res = svc.set_mode(SIM, "hdr")
    assert res.settings.advanced["hdr.enabled"] == "on" and svc.current_mode(SIM) == "hdr"
    assert svc.scoring_params(SIM).target_mean == 0.35  # mode scoring override
    svc.set_mode(SIM, "linear")
    assert svc.scoring_params(SIM).target_mean == 0.45
    with pytest.raises(NotFoundError):
        svc.set_mode(SIM, "nope")


def test_set_mode_rejected_by_limits(
    make_service: Callable[..., TwoDService], tmp_path: Path
) -> None:
    prof = yaml.safe_load((profiles_dir() / "SIM-0001.yaml").read_text())
    prof["modes"]["linear"]["settings"]["gain_db"] = 30.0
    (tmp_path / "SIM-0001.yaml").write_text(yaml.safe_dump(prof))
    svc = make_service(profiles_root=tmp_path)
    svc.open(SIM)
    with pytest.raises(LimitRejectedError, match="cannot enter mode"):
        svc.set_mode(SIM, "linear")
    assert svc.current_mode(SIM) is None


def test_opening_does_not_change_mode(svc: TwoDService) -> None:
    assert svc.get_settings(SIM).settings.advanced["hdr.enabled"] == "off"
    # nothing was entered; the mode is recognised from the settings (a new process, e.g. each
    # CLI call, must still use the active mode's scoring targets — fresh-agent test 2026-10-23)
    assert svc.current_mode(SIM) == "linear"


def test_mode_is_recognised_from_the_settings(svc: TwoDService) -> None:
    svc.apply(SIM, CameraSettingsPatch(advanced={"hdr.enabled": "on"}))
    svc._open[SIM].mode = None  # a new process: nothing entered
    assert svc.current_mode(SIM) == "hdr"
    assert svc.scoring_params(SIM).target_mean == 0.35  # that mode's scoring override


@pytest.mark.parametrize(
    ("mode", "strategy", "target"),
    [("linear", "linear", 0.45), ("hdr", "hdr", 0.35), ("prism", "prism", 0.45)],
)
def test_tune_each_mode_on_sim(svc: TwoDService, mode: str, strategy: str, target: float) -> None:
    svc.apply(
        SIM,
        CameraSettingsPatch(
            exposure_us=2000, advanced={"hdr.ch0.exposure_us": 2000, "hdr.ch1.exposure_us": 2000}
        ),
    )
    r = svc.tune(SIM, mode, white_balance=mode != "hdr", max_steps=20)
    assert (r.mode, r.strategy) == (mode, strategy)
    assert r.converged, r.history
    assert abs((r.final_mean or 0) - target) <= 0.05 * target
    assert r.to_dict()["mode"] == mode


def test_hdr_strategy_moves_channel_exposures_together(svc: TwoDService) -> None:
    svc.apply(
        SIM, CameraSettingsPatch(advanced={"hdr.ch0.exposure_us": 500, "hdr.ch1.exposure_us": 900})
    )
    svc.tune(SIM, "hdr")
    adv = svc.get_settings(SIM).settings.advanced
    assert adv["hdr.ch0.exposure_us"] == adv["hdr.ch1.exposure_us"] != 500
    assert svc.get_settings(SIM).settings.gain_db == 0  # hdr never raises gain


def test_prism_wb_by_exposure_on_sim(svc: TwoDService, sim_light: SimulatedLight) -> None:
    sim_light.set_levels({"red": 80, "white_ring": 30})
    r = svc.tune(SIM, "prism", white_balance=True, max_steps=20)
    s = svc.get_settings(SIM).settings
    assert s.advanced["prism.exposure_mode"] == "individual"
    assert s.white_balance == {"red": 1.0, "green": 1.0, "blue": 1.0}
    assert s.advanced["prism.exposure_red_us"] < s.advanced["prism.exposure_blue_us"]
    m = svc.grab(SIM).data.reshape(-1, 3).astype(float).mean(axis=0)
    assert (m.max() - m.min()) / m.mean() <= 0.03, (m, r.message)


def test_tune_options_and_strategy_override(svc: TwoDService) -> None:
    r = svc.tune(SIM, "prism", white_balance=True, options={"wb_method": "gain"}, max_steps=20)
    s = svc.get_settings(SIM).settings
    assert r.strategy == "prism" and s.advanced["prism.exposure_mode"] == "common"
    with pytest.raises(InvalidRequestError, match="needs advanced"):
        svc.tune(SIM, "linear", strategy="hdr")
    with pytest.raises(InvalidRequestError, match="unknown strategy"):
        svc.tune(SIM, "linear", strategy="magic")
    with pytest.raises(InvalidRequestError, match="wb_method"):
        svc.tune(SIM, "prism", options={"wb_method": "nope"})


def test_bad_profile_file_is_config_error(
    make_service: Callable[..., TwoDService], tmp_path: Path
) -> None:
    (tmp_path / "SIM-0001.yaml").write_text("serial: SIM-0001\n")
    svc = make_service(profiles_root=tmp_path)
    with pytest.raises(ConfigurationError):
        svc.get_profile(SIM)


# ------------------------------------------------------------------- CLI
def _cli(*args: str) -> Any:
    return runner.invoke(app, list(args))


def test_cli_profile_show_and_mode_set() -> None:
    res = _cli("two-d", "profile", "show", "triton")
    assert res.exit_code == 0 and "rolling" in res.stdout and "*raw_hdr" in res.stdout
    data = json.loads(_cli("two-d", "profile", "show", "apex", "--json").stdout)["result"]
    assert data["default_mode"] == "prism"
    res = _cli("--sim", "two-d", "mode", "set", "SIM-0001", "hdr", "--json")
    assert res.exit_code == 0
    assert json.loads(res.stdout)["result"]["settings"]["advanced"]["hdr.enabled"] == "on"
    assert _cli("--sim", "two-d", "mode", "set", "SIM-0001", "nope").exit_code == 2


def test_cli_tune_modes_and_options() -> None:
    out = json.loads(_cli("--sim", "two-d", "tune", "SIM-0001", "--mode", "hdr", "--json").stdout)[
        "result"
    ]
    assert (out["mode"], out["strategy"], out["converged"]) == ("hdr", "hdr", True)
    out2 = json.loads(
        _cli(
            "--sim",
            "two-d",
            "tune",
            "SIM-0001",
            "--mode",
            "prism",
            "--wb",
            "--option",
            "wb_method=gain",
            "--json",
        ).stdout
    )["result"]
    assert out2["strategy"] == "prism"
    assert _cli("--sim", "two-d", "tune", "SIM-0001", "--option", "novalue").exit_code == 2
    assert _cli("--sim", "two-d", "tune", "SIM-0001", "--strategy", "magic").exit_code == 2


# ------------------------------------------------- drivers: HDR / prism
def _service(drivers: dict[str, Any]) -> TwoDService:
    limits = load_safety_limits()
    return TwoDService(limits, LightService(SimulatedLight(), limits), drivers=drivers)


def test_arena_hdr_advanced_on_fake(tmp_roots: Path) -> None:
    system = FakeSystem()
    svc = _service({"arena": ArenaDriver(system)})
    try:
        svc.open("233100505", "test")
        caps = svc.get_settings("233100505")
        assert caps.options["hdr.output"][:2] == ("HDR", "HDRChannel0")
        assert caps.ranges["hdr.ch2.gain_db"].max.value == 38.4  # type: ignore[union-attr]  # camera range
        nm = system.created[-1].nodemap
        res = svc.set_mode("233100505", "hdr")
        assert res.settings.advanced["hdr.enabled"] == "on" and nm["HDRTuningEnable"].value is True
        r = svc.apply(
            "233100505",
            CameraSettingsPatch(
                advanced={
                    "hdr.ch1.exposure_us": 20_000,
                    "hdr.ch1.gain_db": 10.0,
                    "hdr.ch2.gain_db": 25.0,
                }
            ),
        )
        by = _by(r)
        assert by["advanced.hdr.ch1.exposure_us"].status == "applied"
        assert by["advanced.hdr.ch1.gain_db"].status == "applied"
        assert by["advanced.hdr.ch2.gain_db"].status == "applied"  # 25 dB: no camera_lab cap
        assert r.settings.advanced["hdr.ch1.exposure_us"] == 20_000
        assert nm["HDRTuningChannelSelector"].value == 0  # selector restored
        long = svc.apply(
            "233100505", CameraSettingsPatch(advanced={"hdr.ch0.exposure_us": 180_000})
        )
        assert long.ok and any("frame rate lowered" in w for w in long.warnings)
        blocked = svc.apply("233100505", CameraSettingsPatch(gain_db=3, exposure_us=5000))
        assert all(
            x.status == "rejected" and "in its current mode" in (x.reason or "")
            for x in blocked.results
        )
        lin = svc.set_mode("233100505", "linear")
        assert lin.settings.advanced["hdr.enabled"] == "off"
        assert svc.apply("233100505", CameraSettingsPatch(gain_db=3)).ok
    finally:
        svc.shutdown()


def test_arena_hdr_strategy_on_fake(tmp_roots: Path) -> None:
    svc = _service({"arena": ArenaDriver(FakeSystem())})
    try:
        svc.open("233100505", "test")
        r = svc.tune("233100505", "hdr", max_steps=6)
        assert (r.mode, r.strategy) == ("hdr", "hdr")
        adv = svc.get_settings("233100505").settings.advanced
        assert len({adv[f"hdr.ch{i}.exposure_us"] for i in range(4)}) == 1  # moved together
    finally:
        svc.shutdown()


def test_ebus_prism_advanced_on_fake(tmp_roots: Path) -> None:
    sdk = FakeSdk()
    svc = _service({"ebus": EbusDriver(sdk)})
    try:
        svc.open("U330439", "test")
        view = svc.get_settings("U330439")
        assert view.options["prism.exposure_mode"] == ("common", "individual")
        assert view.options["prism.gain_mode"] == ("master", "individual")
        cam = sdk.connected[-1].cam
        r = svc.apply("U330439", CameraSettingsPatch(advanced={"prism.exposure_red_us": 5000}))
        assert (
            "needs prism.exposure_mode = individual"
            in _by(r)["advanced.prism.exposure_red_us"].reason
        )
        r = svc.apply(
            "U330439",
            CameraSettingsPatch(
                advanced={
                    "prism.exposure_mode": "individual",
                    "prism.exposure_red_us": 5000,
                    "prism.exposure_blue_us": 150_000,
                }
            ),
        )
        assert r.ok, r.results
        assert cam.v["ExposureTimeMode"] == "Individual" and cam.channel_exposure["Red"] == 5000
        assert any("frame rate lowered" in w for w in r.warnings)  # blue longer than the frame
        assert cam.v["ExposureTimeSelector"] == "Common"  # selector restored
        svc.grab("U330439")
        r2 = svc.apply("U330439", CameraSettingsPatch(advanced={"prism.exposure_mode": "common"}))
        assert r2.ok  # service stopped the stream (ExposureTimeMode locked while streaming)
        r3 = svc.apply("U330439", CameraSettingsPatch(advanced={"prism.gain_mode": "individual"}))
        assert r3.ok and cam.v["IndividualGainMode"] == "On"
        assert not svc.apply("U330439", CameraSettingsPatch(gain_db=3)).ok  # master gain locked
        bad = svc.apply("U330439", CameraSettingsPatch(advanced={"prism.gain_mode": "sideways"}))
        assert not bad.ok
    finally:
        svc.shutdown()


def test_sim_prism_exposures_render_per_channel(svc: TwoDService) -> None:
    svc.apply(
        SIM,
        CameraSettingsPatch(
            pixel_format="RGB8",
            advanced={
                "prism.exposure_mode": "individual",
                "prism.exposure_red_us": 20_000,
                "prism.exposure_green_us": 5_000,
                "prism.exposure_blue_us": 5_000,
            },
        ),
    )
    m = svc.grab(SIM).data.reshape(-1, 3).astype(float).mean(axis=0)
    assert m[0] > 2.5 * m[1]
    assert np.isclose(m[1], m[2], rtol=0.5)
