"""Tests for core.safety: limits model, effective ranges, checks, rate limits, budgets."""

from __future__ import annotations

import hashlib
import math
from pathlib import Path
from typing import Any

import pydantic
import pytest
import yaml

from camera_lab.core import safety
from camera_lab.core.safety import (
    Accepted,
    Bound,
    BudgetError,
    BudgetExhaustedError,
    CameraRange,
    Clamped,
    LoadedSafetyLimits,
    NotAdjustable,
    RateLimiter,
    Rejected,
    SafetyConfigError,
    SafetyLimits,
    StepBudget,
    check,
    check_choice,
    effective_range,
    load_safety_limits,
)

REAL_FILE = safety.default_limits_path()


def _base() -> dict[str, Any]:
    return yaml.safe_load(REAL_FILE.read_text())


def _write(tmp_path: Path, data: dict[str, Any]) -> Path:
    p = tmp_path / "safety_limits.yaml"
    p.write_text(yaml.safe_dump(data))
    return p


@pytest.fixture
def loaded() -> LoadedSafetyLimits:
    return load_safety_limits()


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def sleep(self, dt: float) -> None:
        self.t += dt


# ------------------------------------------------------------------ loading
def test_real_file_loads_with_spec_defaults(loaded: LoadedSafetyLimits) -> None:
    lim = loaded.limits
    assert lim.schema_version == 2
    cam = lim.cameras.default
    assert cam.max_writes_per_s == 5 and cam.allowed_pixel_formats is None
    assert cam.protected_features == safety.DEFAULT_PROTECTED_FEATURES
    light = lim.lights.default
    assert (light.max_pct, light.max_step_pct, light.max_changes_per_s) == (80, 25, 2)
    assert lim.tuning.max_steps_hard == 40
    assert lim.tuning.max_captures_per_run == 200
    assert lim.mcp.max_capture_count == 5
    assert loaded.sha256 == hashlib.sha256(REAL_FILE.read_bytes()).hexdigest()
    assert loaded.sha256 == safety.limits_hash(REAL_FILE)
    assert loaded.label == "safety_limits.yaml"


def test_sim_defaults_match_real_file(loaded: LoadedSafetyLimits) -> None:
    assert loaded.limits == safety.SIM_DEFAULT_LIMITS


def test_missing_file_refuses_to_start(tmp_path: Path) -> None:
    with pytest.raises(SafetyConfigError, match="refusing to start"):
        load_safety_limits(tmp_path / "missing.yaml")


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.pop("tuning"),
        lambda d: d.update(schema_version=3),
        lambda d: d.update(extra_key=1),
        lambda d: d["cameras"]["default"].update(exposure_us={"min": 20, "max": 200_000}),
        lambda d: d["cameras"]["default"].update(protected_features=["Gev Persistent!"]),
        lambda d: d["cameras"]["default"].update(max_writes_per_s=0),
        lambda d: d["lights"]["default"].update(max_pct=150),
        lambda d: d["lights"]["default"].update(max_step_pct=0),
        lambda d: d["lights"]["default"].update(max_pct=".nan"),
        lambda d: d["cameras"].update({"bad_serial": {}}),
        lambda d: d["cameras"].update({"overrides": {}}),
        lambda d: d["lights"].update({"Bad Channel": {}}),
        lambda d: d["tuning"].update(max_steps_hard=0),
    ],
)
def test_invalid_file_refuses_to_start(tmp_path: Path, mutate: Any) -> None:
    data = _base()
    mutate(data)
    p = _write(tmp_path, data)
    with pytest.raises(SafetyConfigError):
        load_safety_limits(p)


def test_nan_written_as_yaml_float_is_rejected(tmp_path: Path) -> None:
    p = tmp_path / "s.yaml"
    p.write_text(REAL_FILE.read_text().replace("max_pct: 80 ", "max_pct: .nan "))
    with pytest.raises(SafetyConfigError, match="max_pct"):
        load_safety_limits(p)


def test_schema_1_is_refused_with_the_policy_change(tmp_path: Path) -> None:
    old = {
        "schema_version": 1,
        "cameras": {"default": {"exposure_us": {"min": 20, "max": 200_000}, "max_writes_per_s": 5}},
        "lights": _base()["lights"],
        "tuning": _base()["tuning"],
        "mcp": _base()["mcp"],
    }
    with pytest.raises(SafetyConfigError, match="schema_version 1 is no longer accepted"):
        load_safety_limits(_write(tmp_path, old))
    # --sim falls back to the built-in defaults, as for any invalid file
    assert load_safety_limits(_write(tmp_path, old), sim=True).source == "builtin-sim"


def test_sim_falls_back_to_builtin_defaults(tmp_path: Path) -> None:
    loaded = load_safety_limits(tmp_path / "missing.yaml", sim=True)
    assert loaded.source == "builtin-sim"
    assert loaded.limits == safety.SIM_DEFAULT_LIMITS
    assert loaded.label == "builtin sim defaults"
    assert len(loaded.sha256) == 64


def test_sim_uses_valid_file(tmp_path: Path) -> None:
    data = _base()
    data["lights"]["default"]["max_pct"] = 60
    loaded = load_safety_limits(_write(tmp_path, data), sim=True)
    assert loaded.limits.lights.default.max_pct == 60


# ------------------------------------------------------------------ frozen
def test_limits_model_is_frozen(loaded: LoadedSafetyLimits) -> None:
    lim = loaded.limits
    with pytest.raises(pydantic.ValidationError):
        lim.cameras.default.max_writes_per_s = 99  # type: ignore[misc]
    with pytest.raises(pydantic.ValidationError):
        lim.tuning = lim.tuning  # type: ignore[misc]
    with pytest.raises(pydantic.ValidationError):
        lim.lights.default.max_pct = 100  # type: ignore[misc]
    with pytest.raises(TypeError):
        lim.cameras.overrides["X"] = None  # type: ignore[index]
    with pytest.raises(TypeError):
        lim.lights.overrides["red"] = None  # type: ignore[index]
    for model in (
        SafetyLimits,
        safety.CameraLimits,
        safety.Range,
        safety.LightLimits,
        safety.TuningLimits,
        safety.McpLimits,
    ):
        assert model.model_config.get("frozen") is True
    with pytest.raises(AttributeError):
        loaded.limits = safety.SIM_DEFAULT_LIMITS  # type: ignore[misc]


def test_no_setter_api() -> None:
    """The module exposes no function that writes or mutates limits."""
    names = [n for n in dir(safety) if not n.startswith("_")]
    assert not [n for n in names if n.lower().startswith(("set", "update", "write", "save"))]


def test_overrides_are_immutable_and_dump(tmp_path: Path) -> None:
    data = _base()
    data["cameras"]["U330439"] = {"max_writes_per_s": 3}
    data["lights"]["red"] = {"max_pct": 60}
    loaded = load_safety_limits(_write(tmp_path, data))
    dumped = loaded.limits.model_dump()
    assert dumped["cameras"]["overrides"]["U330439"]["max_writes_per_s"] == 3


# ---------------------------------------------------------------- resolved
def test_camera_override_and_key_paths(tmp_path: Path) -> None:
    data = _base()
    data["cameras"]["U330439"] = {"max_writes_per_s": 3, "protected_features": ["Gev*"]}
    loaded = load_safety_limits(_write(tmp_path, data))
    jai = loaded.camera("U330439")
    assert jai.max_writes_per_s == 3
    assert jai.source("max_writes_per_s") == "safety_limits.yaml: cameras.U330439.max_writes_per_s"
    assert jai.is_protected("GevSCPSPacketSize") and not jai.is_protected("DeviceReset")
    other = loaded.camera("233100505")
    assert other.max_writes_per_s == 5
    assert other.source("allowed_pixel_formats") == (
        "safety_limits.yaml: cameras.default.allowed_pixel_formats"
    )


def test_default_protected_features(loaded: LoadedSafetyLimits) -> None:
    lim = loaded.camera("233100505")
    for name in (
        "GevPersistentIPAddress",
        "GevCurrentIPConfigurationDHCP",
        "DeviceReset",
        "FileAccessControl",
        "UserSetSave",
        "UserSetDefault",
    ):
        assert lim.is_protected(name), name
    for name in ("ExposureTime", "Gain", "UserSetSelector", "UserSetLoad", "PixelFormat"):
        assert not lim.is_protected(name), name


def test_light_override(tmp_path: Path) -> None:
    data = _base()
    data["lights"]["red"] = {"max_pct": 60}
    loaded = load_safety_limits(_write(tmp_path, data))
    red = loaded.light("red")
    assert red.max_pct == 60
    assert red.max_step_pct == 25
    assert red.source("max_pct") == "safety_limits.yaml: lights.red.max_pct"
    assert loaded.light("blue").source("max_pct") == "safety_limits.yaml: lights.default.max_pct"


# ---------------------------------------------------------- effective range
def test_spec_example_rejection_message(loaded: LoadedSafetyLimits) -> None:
    rng = loaded.camera("SIM-0001").range_for("gain_db", CameraRange(0, 42, None))
    assert check(30.0, rng) == Accepted(30.0)  # schema 2: no camera_lab cap
    res = check(45.0, rng)
    assert isinstance(res, Rejected)
    assert res.reason == "gain_db 45.0 > camera max 42.0"


def test_camera_range_is_the_effective_range(loaded: LoadedSafetyLimits) -> None:
    lim = loaded.camera("SIM-0001")
    rng = lim.range_for("exposure_us", CameraRange(50, 10_000_000, None))
    assert isinstance(rng, safety.EffectiveRange)
    assert rng.min.value == 50 and rng.min.source == "camera min"
    assert rng.max.value == 10_000_000 and rng.max.source == "camera max"
    adv = lim.advanced_range("hdr.ch2.gain_db", "dB", CameraRange(0, 38.4, None))
    assert isinstance(adv, safety.EffectiveRange) and adv.max.value == 38.4
    fr = lim.range_for("frame_rate_hz", CameraRange(1, 24, None))
    assert isinstance(fr, safety.EffectiveRange)
    assert fr.max.value == 24 and fr.max.source == "camera max"


def test_effective_range_snaps_to_increment() -> None:
    rng = effective_range(
        "exposure_us",
        CameraRange(1.0, 1000.0, 3.0),
        Bound(20, "f: a.min"),
        Bound(200, "f: a.max"),
    )
    assert isinstance(rng, safety.EffectiveRange)
    assert rng.min.value == 22.0  # 1 + 7*3, first grid point >= 20
    assert rng.max.value == 199.0  # 1 + 66*3, last grid point <= 200


def test_empty_intersection_not_adjustable() -> None:
    rng = effective_range("gain_db", CameraRange(20, 40, None), None, Bound(18, "f: g.max"))
    assert isinstance(rng, NotAdjustable)
    assert "not adjustable" in rng.reason
    res = check(25, rng)
    assert isinstance(res, Rejected) and "not adjustable" in res.reason


def test_empty_after_snapping() -> None:
    rng = effective_range(
        "x", CameraRange(0, 100, 10), Bound(11, "f: x.min"), Bound(19, "f: x.max")
    )
    assert isinstance(rng, NotAdjustable)


# ----------------------------------------------------------------- check()
@pytest.fixture
def gain() -> safety.EffectiveRange:
    rng = effective_range(
        "gain_db", CameraRange(0, 24, 0.1), Bound(0, "f.yaml: g.min"), Bound(18, "f.yaml: g.max")
    )
    assert isinstance(rng, safety.EffectiveRange)
    return rng


def test_check_accepts_inside(gain: safety.EffectiveRange) -> None:
    assert check(6.0, gain) == Accepted(6.0)
    assert check(0, gain) == Accepted(0.0)
    assert check(18, gain) == Accepted(18.0)


def test_check_rejects_below_with_source(gain: safety.EffectiveRange) -> None:
    res = check(-1.0, gain)
    assert isinstance(res, Rejected)
    assert res.reason == "gain_db -1.0 < limit 0.0 (f.yaml: g.min)"


def test_check_camera_bound_reason() -> None:
    rng = effective_range("gain_db", CameraRange(0, 12), None, Bound(18, "f.yaml: g.max"))
    res = check(13, rng)
    assert isinstance(res, Rejected)
    assert res.reason == "gain_db 13.0 > camera max 12.0"


def test_check_clamp_mode(gain: safety.EffectiveRange) -> None:
    hi = check(30, gain, mode="clamp")
    assert isinstance(hi, Clamped) and hi.value == 18 and hi.requested == 30
    assert "limit 18.0" in hi.reason
    lo = check(-5, gain, mode="clamp")
    assert isinstance(lo, Clamped) and lo.value == 0


def test_check_rounds_to_increment(gain: safety.EffectiveRange) -> None:
    res = check(6.04, gain)
    assert isinstance(res, Clamped)
    assert res.value == pytest.approx(6.0)
    assert "increment" in res.reason
    assert check(6.1, gain) == Accepted(6.1)


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf, "6", None, True, [1], 10**400])
def test_check_rejects_non_numbers(gain: safety.EffectiveRange, bad: Any) -> None:
    res = check(bad, gain)
    assert isinstance(res, Rejected)
    assert "not a finite number" in res.reason


def test_check_choice(loaded: LoadedSafetyLimits, tmp_path: Path) -> None:
    opts = ["Mono8", "BayerRG8", "RGB8"]
    lim = loaded.camera("SIM-0001")
    assert lim.check_pixel_format("RGB8", opts) == Accepted("RGB8")
    res = lim.check_pixel_format("Mono16", opts)
    assert isinstance(res, Rejected) and "not supported by camera" in res.reason
    assert isinstance(lim.check_pixel_format(8, opts), Rejected)

    data = _base()
    data["cameras"]["default"]["allowed_pixel_formats"] = ["BayerRG8"]
    restricted = load_safety_limits(_write(tmp_path, data)).camera("X1")
    res2 = restricted.check_pixel_format("RGB8", opts)
    assert isinstance(res2, Rejected)
    assert "cameras.default.allowed_pixel_formats" in res2.reason
    assert check_choice("pf", "a", ["a"], None, "") == Accepted("a")


def test_light_level_range(loaded: LoadedSafetyLimits) -> None:
    rng = loaded.light("white_ring").level_range()
    assert check(80, rng) == Accepted(80.0)
    res = check(90, rng)
    assert isinstance(res, Rejected)
    assert res.reason == (
        "white_ring level_pct 90.0 > limit 80.0 (safety_limits.yaml: lights.default.max_pct)"
    )
    assert isinstance(check(-1, rng), Rejected)
    low_hw = loaded.light("x").level_range(hardware_max_pct=50)
    res2 = check(60, low_hw)
    assert isinstance(res2, Rejected) and "channel max 50.0" in res2.reason


# ------------------------------------------------------------ rate limiter
def test_rate_limiter_burst_then_rate() -> None:
    clk = FakeClock()
    rl = RateLimiter(5, clock=clk)
    assert [rl.try_acquire() for _ in range(6)] == [True] * 5 + [False]
    assert rl.time_until_available() == pytest.approx(0.2)
    clk.t += 0.2
    assert rl.try_acquire()
    assert not rl.try_acquire()
    clk.t += 10
    assert sum(rl.try_acquire() for _ in range(20)) == 5  # capped at burst


def test_rate_limiter_acquire_sleeps_with_fake_clock() -> None:
    clk = FakeClock()
    rl = RateLimiter(2, clock=clk)
    start = clk.t
    for _ in range(6):
        rl.acquire(sleep=clk.sleep)
    # 2 immediate (burst), then 4 more at 2/s -> 2 s
    assert clk.t - start == pytest.approx(2.0)


def test_rate_limiter_clock_going_backwards_is_safe() -> None:
    clk = FakeClock()
    rl = RateLimiter(1, clock=clk)
    assert rl.try_acquire()
    clk.t -= 100
    assert not rl.try_acquire()


@pytest.mark.parametrize(("rate", "burst"), [(0, None), (-1, None), (math.inf, None), (1, 0.5)])
def test_rate_limiter_bad_args(rate: float, burst: float | None) -> None:
    with pytest.raises(ValueError):
        RateLimiter(rate, burst)


# ---------------------------------------------------------------- budgets
def test_step_budget() -> None:
    b = StepBudget(3, 40)
    assert b.use() == 2
    assert b.use(2) == 0
    assert b.exhausted and b.used == 3 and b.remaining == 0
    with pytest.raises(BudgetExhaustedError, match="3/3"):
        b.use()
    assert b.used == 3
    with pytest.raises(ValueError):
        b.use(0)


def test_step_budget_partial_overdraw_consumes_nothing() -> None:
    b = StepBudget(3, 40)
    b.use()
    with pytest.raises(BudgetExhaustedError):
        b.use(3)
    assert b.used == 1


@pytest.mark.parametrize("bad", [0, -1, 41, True, 2.5])
def test_step_budget_limits(bad: Any) -> None:
    with pytest.raises(BudgetError):
        StepBudget(bad, 40)


def test_step_budget_from_limits(loaded: LoadedSafetyLimits) -> None:
    assert loaded.step_budget(40).max_steps == 40
    with pytest.raises(
        BudgetError, match=r"41 > limit 40 \(safety_limits.yaml: tuning.max_steps_hard\)"
    ):
        loaded.step_budget(41)
    cap = loaded.capture_budget()
    assert cap.max_steps == 200 and cap.name == "captures"
