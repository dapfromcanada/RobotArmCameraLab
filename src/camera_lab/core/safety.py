"""Safety limits: the only privilege boundary in camera_lab.

SECURITY-SENSITIVE. Every code path that changes a camera setting or a light
level (CLI, MCP, GUI, classic tuner) goes through this module. There are no
bypass flags.

- Limits are loaded once from ``APP/config/safety_limits.yaml`` into a frozen
  pydantic model. There is no setter and no API to change them at runtime;
  changing them needs a human to edit the file and restart the process.
- A missing or invalid file raises :class:`SafetyConfigError` (fail closed).
  Only ``sim=True`` falls back to the built-in simulation defaults.
- **Limits policy (Derek, 2026-10-21, schema 2):** camera exposure, gain and
  frame rate are limited only by the camera's own range (they cannot damage a
  camera); camera_lab does not narrow them. Hard limits stay on lights, rates
  and budgets, and on **protected camera features** (network/IP, reset,
  firmware/file access, user-set save), which camera_lab never writes through
  its settings tools or a restore. A camera profile's ``working_range`` steers
  tuning but never blocks.
- The effective range of a light level is the intersection of the hardware
  range and the limit; of a camera setting, the camera-reported range
  (:func:`effective_range`).
- :func:`check` returns :class:`Accepted`, :class:`Clamped` or :class:`Rejected`.
  Out-of-range values are rejected (``mode="reject"``, used by set_camera_settings
  and set_lights) or clamped (``mode="clamp"``, used only by the classic tuner).
  Values off the camera increment are rounded and reported as clamped. Reasons
  quote the source, e.g. ``gain_db 45.0 > camera max 42.0`` or
  ``red level_pct 90.0 > limit 80.0 (safety_limits.yaml: lights.default.max_pct)``.
"""

from __future__ import annotations

import fnmatch
import hashlib
import math
import re
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)

from camera_lab.core.config import ConfigError, load_yaml_model
from camera_lab.core.logging import get_logger
from camera_lab.core.paths import SERIAL_RE, app_config_dir

SAFETY_LIMITS_FILENAME = "safety_limits.yaml"
#: Channel ids in lights.yaml / safety_limits.yaml.
CHANNEL_ID_RE = r"[a-z0-9_-]{1,40}"

_log = get_logger(__name__)


class SafetyConfigError(RuntimeError):
    """The safety-limits file is missing or invalid; the app must not start."""


class BudgetError(ValueError):
    """A requested step/capture budget exceeds its hard limit."""


class BudgetExhaustedError(RuntimeError):
    """A step/capture budget has been used up."""


# ===================================================================== models
class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class Range(_Frozen):
    """A closed numeric range ``[min, max]``."""

    min: float
    max: float

    @model_validator(mode="after")
    def _ordered(self) -> Range:
        if self.min > self.max:
            raise ValueError(f"min {self.min} > max {self.max}")
        return self


class MaxOnly(_Frozen):
    """An upper bound only."""

    max: float = Field(gt=0)


#: Protected feature patterns: GenICam node names with shell wildcards.
PROTECTED_PATTERN_RE = re.compile(r"[A-Za-z0-9_*?\[\]]{1,64}")
#: Never written through settings tools or a restore (Instructions: Limits policy).
DEFAULT_PROTECTED_FEATURES: tuple[str, ...] = (
    "GevPersistent*",
    "GevCurrentIPConfiguration*",
    "DeviceReset",
    "DeviceFactoryReset",
    "FileAccess*",
    "FirmwareUpdate*",
    "UserSetSave",
    "UserSetDefault*",
)
SCHEMA_1_MESSAGE = (
    "schema_version 1 is no longer accepted: since 2026-10-21 (Derek) camera exposure, gain "
    "and frame rate are limited only by the camera's own range, so the cameras.*.exposure_us / "
    "gain_db / frame_rate_hz / advanced caps were removed; working ranges now live in the camera "
    "profiles. Update the file to schema_version 2 (see the comments in "
    "config/safety_limits.yaml)"
)


def _check_protected(v: tuple[str, ...]) -> tuple[str, ...]:
    bad = [k for k in v if PROTECTED_PATTERN_RE.fullmatch(k) is None]
    if bad:
        raise ValueError(f"invalid protected feature patterns: {', '.join(bad)}")
    return v


class CameraLimits(_Frozen):
    """Limits for one camera (``cameras.default``)."""

    allowed_pixel_formats: tuple[str, ...] | None = None
    max_writes_per_s: float = Field(gt=0, le=1000)
    protected_features: tuple[str, ...] = DEFAULT_PROTECTED_FEATURES

    @field_validator("protected_features")
    @classmethod
    def _patterns(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        return _check_protected(v)


class CameraLimitsOverride(_Frozen):
    """Per-camera override (``cameras.<serial>``); unset fields use the default."""

    allowed_pixel_formats: tuple[str, ...] | None = None
    max_writes_per_s: float | None = Field(default=None, gt=0, le=1000)
    protected_features: tuple[str, ...] | None = None

    @field_validator("protected_features")
    @classmethod
    def _patterns(cls, v: tuple[str, ...] | None) -> tuple[str, ...] | None:
        return None if v is None else _check_protected(v)


class LightLimits(_Frozen):
    """Limits for one light channel (``lights.default``)."""

    max_pct: float = Field(ge=0, le=100)
    max_step_pct: float = Field(gt=0, le=100)
    max_changes_per_s: float = Field(gt=0, le=100)


class LightLimitsOverride(_Frozen):
    """Per-channel override (``lights.<channel_id>``)."""

    max_pct: float | None = Field(default=None, ge=0, le=100)
    max_step_pct: float | None = Field(default=None, gt=0, le=100)
    max_changes_per_s: float | None = Field(default=None, gt=0, le=100)


def _split_default(data: Any, pattern: str, what: str) -> Any:
    """Turn ``{default: ..., <key>: ...}`` into ``{default: ..., overrides: {...}}``."""
    if not isinstance(data, Mapping):
        return data
    rx = re.compile(pattern)
    overrides = {}
    out: dict[str, Any] = {}
    for key, value in data.items():
        if key == "default":
            out["default"] = value
        elif isinstance(key, str) and rx.fullmatch(key):
            overrides[key] = value
        else:
            raise ValueError(f"invalid {what} key {key!r}: expected 'default' or {pattern}")
    out["overrides"] = overrides
    return out


class CamerasLimits(_Frozen):
    """``cameras:`` section: ``default`` plus per-serial overrides."""

    default: CameraLimits
    overrides: Mapping[str, CameraLimitsOverride] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _split(cls, data: Any) -> Any:
        if isinstance(data, Mapping) and "overrides" in data:
            raise ValueError("'overrides' is not a valid key; use cameras.<serial>")
        return _split_default(data, SERIAL_RE.pattern, "camera serial")

    @field_validator("overrides", mode="after")
    @classmethod
    def _freeze(cls, v: Mapping[str, CameraLimitsOverride]) -> Mapping[str, CameraLimitsOverride]:
        return MappingProxyType(dict(v))

    @field_serializer("overrides")
    def _dump_overrides(
        self, v: Mapping[str, CameraLimitsOverride]
    ) -> dict[str, CameraLimitsOverride]:
        return dict(v)


class LightsLimits(_Frozen):
    """``lights:`` section: ``default`` plus per-channel overrides."""

    default: LightLimits
    overrides: Mapping[str, LightLimitsOverride] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _split(cls, data: Any) -> Any:
        if isinstance(data, Mapping) and "overrides" in data:
            raise ValueError("'overrides' is not a valid key; use lights.<channel_id>")
        return _split_default(data, CHANNEL_ID_RE, "light channel")

    @field_validator("overrides", mode="after")
    @classmethod
    def _freeze(cls, v: Mapping[str, LightLimitsOverride]) -> Mapping[str, LightLimitsOverride]:
        return MappingProxyType(dict(v))

    @field_serializer("overrides")
    def _dump_overrides(
        self, v: Mapping[str, LightLimitsOverride]
    ) -> dict[str, LightLimitsOverride]:
        return dict(v)


class TuningLimits(_Frozen):
    """``tuning:`` section."""

    max_steps_hard: int = Field(ge=1, le=1000)
    max_captures_per_run: int = Field(ge=1, le=100_000)


class McpLimits(_Frozen):
    """``mcp:`` section."""

    max_capture_count: int = Field(ge=1, le=100)


class SafetyLimits(_Frozen):
    """The whole ``safety_limits.yaml`` file. Frozen: there is no way to change it."""

    @model_validator(mode="before")
    @classmethod
    def _old_schema(cls, data: Any) -> Any:
        if isinstance(data, Mapping) and data.get("schema_version") == 1:
            raise ValueError(SCHEMA_1_MESSAGE)
        return data

    schema_version: Literal[2]
    cameras: CamerasLimits
    lights: LightsLimits
    tuning: TuningLimits
    mcp: McpLimits


#: Built-in limits used only with ``--sim`` when the file is missing or invalid.
SIM_DEFAULT_LIMITS = SafetyLimits.model_validate(
    {
        "schema_version": 2,
        "cameras": {"default": {"allowed_pixel_formats": None, "max_writes_per_s": 5}},
        "lights": {"default": {"max_pct": 80, "max_step_pct": 25, "max_changes_per_s": 2}},
        "tuning": {"max_steps_hard": 40, "max_captures_per_run": 200},
        "mcp": {"max_capture_count": 5},
    }
)


# ============================================================ resolved views
@dataclass(frozen=True)
class ResolvedCameraLimits:
    """Limits for one camera after applying its override; knows each key path."""

    serial: str
    allowed_pixel_formats: tuple[str, ...] | None
    max_writes_per_s: float
    protected_features: tuple[str, ...]
    label: str
    keys: Mapping[str, str]

    def source(self, name: str, bound: str | None = None) -> str:
        """Return e.g. ``safety_limits.yaml: cameras.default.max_writes_per_s``."""
        key = self.keys[name] + (f".{bound}" if bound else "")
        return f"{self.label}: {key}"

    def range_for(
        self, name: Literal["exposure_us", "gain_db", "frame_rate_hz"], camera: CameraRange
    ) -> EffectiveRange | NotAdjustable:
        """Effective range of a core camera setting: the camera's own range (schema 2)."""
        return effective_range(name, camera, None, None)

    def advanced_range(
        self, name: str, unit: str, camera: CameraRange
    ) -> EffectiveRange | NotAdjustable:
        """Effective range of an advanced setting: the camera's own range (schema 2)."""
        return effective_range(name, camera, None, None)

    def is_protected(self, feature: str) -> bool:
        """True if the GenICam feature matches a ``protected_features`` pattern."""
        return any(fnmatch.fnmatchcase(feature, pat) for pat in self.protected_features)

    def check_pixel_format(self, value: str, camera_options: Sequence[str]) -> CheckResult:
        """Check a pixel format against the camera's options and the allowed list."""
        return check_choice(
            "pixel_format",
            value,
            camera_options,
            self.allowed_pixel_formats,
            self.source("allowed_pixel_formats"),
        )


@dataclass(frozen=True)
class ResolvedLightLimits:
    """Limits for one light channel after applying its override."""

    channel_id: str
    max_pct: float
    max_step_pct: float
    max_changes_per_s: float
    label: str
    keys: Mapping[str, str]

    def source(self, name: str) -> str:
        """Return e.g. ``safety_limits.yaml: lights.default.max_pct``."""
        return f"{self.label}: {self.keys[name]}"

    def level_range(self, hardware_max_pct: float = 100.0) -> EffectiveRange | NotAdjustable:
        """Effective level range: ``[0, min(hardware max, limit max_pct)]``."""
        return effective_range(
            f"{self.channel_id} level_pct",
            CameraRange(0.0, hardware_max_pct),
            None,
            Bound(self.max_pct, self.source("max_pct")),
            camera_label="channel",
        )


@dataclass(frozen=True)
class LoadedSafetyLimits:
    """Safety limits plus where they came from and the sha256 of the source."""

    limits: SafetyLimits
    source: str
    sha256: str

    @property
    def label(self) -> str:
        """Short label used in rejection reasons."""
        return Path(self.source).name if self.source != "builtin-sim" else "builtin sim defaults"

    def camera(self, serial: str) -> ResolvedCameraLimits:
        """Return the limits for ``serial`` (default merged with ``cameras.<serial>``)."""
        default = self.limits.cameras.default
        ovr = self.limits.cameras.overrides.get(serial)
        values: dict[str, Any] = {}
        keys: dict[str, str] = {}
        for name in CameraLimits.model_fields:
            ov = getattr(ovr, name) if ovr is not None else None
            if ov is not None:
                values[name] = ov
                keys[name] = f"cameras.{serial}.{name}"
            else:
                values[name] = getattr(default, name)
                keys[name] = f"cameras.default.{name}"
        return ResolvedCameraLimits(
            serial=serial, label=self.label, keys=MappingProxyType(keys), **values
        )

    def light(self, channel_id: str) -> ResolvedLightLimits:
        """Return the limits for ``channel_id`` (default merged with ``lights.<id>``)."""
        default = self.limits.lights.default
        ovr = self.limits.lights.overrides.get(channel_id)
        values: dict[str, Any] = {}
        keys: dict[str, str] = {}
        for name in LightLimits.model_fields:
            ov = getattr(ovr, name) if ovr is not None else None
            if ov is not None:
                values[name] = ov
                keys[name] = f"lights.{channel_id}.{name}"
            else:
                values[name] = getattr(default, name)
                keys[name] = f"lights.default.{name}"
        return ResolvedLightLimits(
            channel_id=channel_id, label=self.label, keys=MappingProxyType(keys), **values
        )

    def step_budget(self, max_steps: int) -> StepBudget:
        """Return a tuning-step budget checked against ``tuning.max_steps_hard``."""
        return StepBudget(
            max_steps,
            self.limits.tuning.max_steps_hard,
            name="steps",
            limit_source=f"{self.label}: tuning.max_steps_hard",
        )

    def capture_budget(self) -> StepBudget:
        """Return a per-run capture budget (``tuning.max_captures_per_run``)."""
        cap = self.limits.tuning.max_captures_per_run
        return StepBudget(
            cap, cap, name="captures", limit_source=f"{self.label}: tuning.max_captures_per_run"
        )


# ==================================================================== loading
def default_limits_path() -> Path:
    """Return ``APP/config/safety_limits.yaml``."""
    return app_config_dir() / SAFETY_LIMITS_FILENAME


def limits_hash(path: Path) -> str:
    """Return the sha256 hex digest of the limits file's bytes."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _sim_defaults() -> LoadedSafetyLimits:
    digest = hashlib.sha256(SIM_DEFAULT_LIMITS.model_dump_json().encode()).hexdigest()
    return LoadedSafetyLimits(SIM_DEFAULT_LIMITS, "builtin-sim", digest)


def load_safety_limits(path: Path | None = None, *, sim: bool = False) -> LoadedSafetyLimits:
    """Load and validate the safety limits. Fail closed.

    Args:
        path: limits file; default ``APP/config/safety_limits.yaml``.
        sim: if True and the file is missing or invalid, use the built-in sim
            defaults instead of failing. A valid file is always used.

    Raises:
        SafetyConfigError: the file is missing or invalid and ``sim`` is False.
    """
    p = Path(path) if path is not None else default_limits_path()
    try:
        limits = load_yaml_model(p, SafetyLimits)
        digest = limits_hash(p)
    except (ConfigError, OSError) as exc:
        if sim:
            _log.warning("safety_limits_fallback_to_sim_defaults", path=str(p), error=str(exc))
            return _sim_defaults()
        raise SafetyConfigError(f"refusing to start: safety limits invalid: {exc}") from None
    return LoadedSafetyLimits(limits, str(p), digest)


# ============================================================ range checking
@dataclass(frozen=True)
class CameraRange:
    """A range reported by the camera (or channel hardware)."""

    min: float
    max: float
    increment: float | None = None


@dataclass(frozen=True)
class Bound:
    """One bound of an effective range and where it comes from."""

    value: float
    source: str


@dataclass(frozen=True)
class EffectiveRange:
    """Intersection of the camera range and the safety limit."""

    name: str
    min: Bound
    max: Bound
    increment: float | None = None
    origin: float = 0.0  # increment grid origin (the camera minimum)
    camera_label: str = "camera"

    def contains(self, value: float) -> bool:
        """Return True if ``value`` lies inside ``[min, max]``."""
        return self.min.value <= value <= self.max.value


@dataclass(frozen=True)
class NotAdjustable:
    """The camera range and the safety limit do not intersect."""

    name: str
    reason: str


@dataclass(frozen=True)
class Accepted:
    """The value is valid and is applied unchanged."""

    value: float | str


@dataclass(frozen=True)
class Clamped:
    """The value was changed (clamped or rounded to the increment) before applying."""

    value: float
    requested: float
    reason: str


@dataclass(frozen=True)
class Rejected:
    """The value is not applied."""

    requested: Any
    reason: str


CheckResult = Accepted | Clamped | Rejected


def _fmt(x: float) -> str:
    return repr(float(x))


def _snap(
    value: float, origin: float, inc: float, direction: Literal["near", "up", "down"]
) -> float:
    k = (value - origin) / inc
    n = {"near": round(k), "up": math.ceil(k - 1e-9), "down": math.floor(k + 1e-9)}[direction]
    return float(round(origin + n * inc, 9))


def effective_range(
    name: str,
    camera: CameraRange,
    limit_min: Bound | None,
    limit_max: Bound | None,
    *,
    camera_label: str = "camera",
) -> EffectiveRange | NotAdjustable:
    """Intersect the camera range with the safety limit.

    The tighter bound wins on each side (the limit wins a tie), and remembers
    its source (a YAML key path, or the camera). With an increment, the bounds are snapped onto the
    camera's grid (min up, max down). An empty result is :class:`NotAdjustable`.
    """
    lo = Bound(camera.min, f"{camera_label} min")
    hi = Bound(camera.max, f"{camera_label} max")
    if limit_min is not None and limit_min.value >= lo.value:
        lo = limit_min
    if limit_max is not None and limit_max.value <= hi.value:
        hi = limit_max
    inc = camera.increment if camera.increment and camera.increment > 0 else None
    if inc is not None:
        lo = Bound(_snap(lo.value, camera.min, inc, "up"), lo.source)
        hi = Bound(_snap(hi.value, camera.min, inc, "down"), hi.source)
    if lo.value > hi.value:
        return NotAdjustable(
            name,
            f"{name} not adjustable: {camera_label} range [{_fmt(camera.min)}, {_fmt(camera.max)}]"
            f" does not intersect limit [{_fmt(lo.value)} ({lo.source}),"
            f" {_fmt(hi.value)} ({hi.source})]",
        )
    return EffectiveRange(name, lo, hi, inc, float(camera.min), camera_label)


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _bound_label(bound: Bound) -> str:
    """``limit`` for a YAML-sourced bound, else the camera label (``camera max``)."""
    return "limit" if ":" in bound.source else bound.source


def check(
    value: Any,
    rng: EffectiveRange | NotAdjustable,
    *,
    mode: Literal["reject", "clamp"] = "reject",
) -> CheckResult:
    """Check ``value`` against an effective range.

    - not a finite number, or range not adjustable → :class:`Rejected`
    - outside the range → :class:`Rejected` (``mode="reject"``) or
      :class:`Clamped` to the violated bound (``mode="clamp"``)
    - off the increment grid → :class:`Clamped` to the nearest valid value
    - otherwise → :class:`Accepted`
    """
    if isinstance(rng, NotAdjustable):
        return Rejected(value, rng.reason)
    try:
        v = float(value) if _is_number(value) else math.nan
    except OverflowError:  # e.g. an int with 400 digits
        v = math.nan
    if not math.isfinite(v):
        shown = repr(value) if len(repr(value)) <= 40 else repr(value)[:37] + "..."
        return Rejected(value, f"{rng.name} {shown} is not a finite number")
    if v > rng.max.value or v < rng.min.value:
        if v > rng.max.value:
            bound, op = rng.max, ">"
        else:
            bound, op = rng.min, "<"
        label = _bound_label(bound)
        src = f" ({bound.source})" if label == "limit" else ""
        reason = f"{rng.name} {_fmt(v)} {op} {label} {_fmt(bound.value)}{src}"
        if mode == "reject":
            return Rejected(value, reason)
        return Clamped(bound.value, v, f"clamped: {reason}")
    if rng.increment is not None:
        snapped = _snap(v, rng.origin, rng.increment, "near")
        snapped = min(max(snapped, rng.min.value), rng.max.value)
        if not math.isclose(snapped, v, rel_tol=1e-9, abs_tol=1e-12):
            return Clamped(
                snapped,
                v,
                f"{rng.name} {_fmt(v)} rounded to {rng.camera_label} increment"
                f" {_fmt(rng.increment)} -> {_fmt(snapped)}",
            )
    return Accepted(v)


def check_choice(
    name: str,
    value: Any,
    camera_options: Sequence[str],
    allowed: Sequence[str] | None,
    allowed_source: str,
) -> CheckResult:
    """Check an enumerated value (e.g. pixel format) against camera options and limits."""
    if not isinstance(value, str):
        return Rejected(value, f"{name} {value!r} is not a string")
    if value not in camera_options:
        options = ", ".join(camera_options)
        return Rejected(value, f"{name} {value!r} not supported by camera ({options})")
    if allowed is not None and value not in allowed:
        return Rejected(
            value, f"{name} {value!r} not in allowed list [{', '.join(allowed)}] ({allowed_source})"
        )
    return Accepted(value)


# ============================================================ rate + budgets
@dataclass
class RateLimiter:
    """Token bucket: ``rate_per_s`` sustained, ``burst`` tokens at most.

    The clock and sleep functions are injectable for tests. Thread-safe.
    """

    rate_per_s: float
    burst: float | None = None
    clock: Callable[[], float] = time.monotonic
    _tokens: float = field(init=False)
    _last: float = field(init=False)
    _lock: threading.Lock = field(init=False, default_factory=threading.Lock)

    def __post_init__(self) -> None:
        if not (self.rate_per_s > 0 and math.isfinite(self.rate_per_s)):
            raise ValueError("rate_per_s must be a positive finite number")
        if self.burst is None:
            self.burst = max(1.0, self.rate_per_s)
        if self.burst < 1:
            raise ValueError("burst must be >= 1")
        self._tokens = float(self.burst)
        self._last = self.clock()

    def _refill(self) -> None:
        now = self.clock()
        elapsed = max(0.0, now - self._last)
        self._last = now
        cap = float(self.burst) if self.burst is not None else 1.0
        self._tokens = min(cap, self._tokens + elapsed * self.rate_per_s)

    def try_acquire(self) -> bool:
        """Take one token if available; return whether it was taken."""
        with self._lock:
            self._refill()
            if self._tokens >= 1.0 - 1e-9:
                self._tokens -= 1.0
                return True
            return False

    def time_until_available(self) -> float:
        """Seconds until one token is available (0 if one is available now)."""
        with self._lock:
            self._refill()
            return max(0.0, (1.0 - self._tokens) / self.rate_per_s)

    def acquire(self, sleep: Callable[[float], None] = time.sleep) -> float:
        """Block (via ``sleep``) until a token is taken; return the total time waited."""
        waited = 0.0
        while not self.try_acquire():
            delay = self.time_until_available()
            sleep(delay)
            waited += delay
        return waited


class StepBudget:
    """A bounded counter for tuning steps or captures per run. Thread-safe."""

    def __init__(
        self, max_steps: int, hard_limit: int, *, name: str = "steps", limit_source: str = ""
    ) -> None:
        if isinstance(max_steps, bool) or not isinstance(max_steps, int) or max_steps < 1:
            raise BudgetError(f"max_{name} must be an integer >= 1, got {max_steps!r}")
        if max_steps > hard_limit:
            src = f" ({limit_source})" if limit_source else ""
            raise BudgetError(f"max_{name} {max_steps} > limit {hard_limit}{src}")
        self.name = name
        self.max_steps = max_steps
        self._used = 0
        self._lock = threading.Lock()

    @property
    def used(self) -> int:
        """Steps used so far."""
        return self._used

    @property
    def remaining(self) -> int:
        """Steps left."""
        return self.max_steps - self._used

    @property
    def exhausted(self) -> bool:
        """True when no steps are left."""
        return self._used >= self.max_steps

    def use(self, n: int = 1) -> int:
        """Consume ``n`` steps; return the remaining count.

        Raises:
            BudgetExhaustedError: if fewer than ``n`` steps are left (nothing is consumed).
        """
        if n < 1:
            raise ValueError("n must be >= 1")
        with self._lock:
            if self._used + n > self.max_steps:
                raise BudgetExhaustedError(
                    f"{self.name} budget exhausted: {self._used}/{self.max_steps} used"
                )
            self._used += n
            return self.max_steps - self._used
