"""Camera profiles: what a camera is, its operating modes and how to tune each mode.

``APP/config/camera_profiles/<serial>.yaml`` (git-tracked, human-edited, loaded
with ``yaml.safe_load`` + pydantic). A camera without a file gets
:func:`generic_profile` (one ``linear`` mode). The library of profiles grows as
cameras are added and compared.

A mode names a tuning ``strategy`` (registered in ``two_d/strategies``), the
``settings`` patch applied when entering the mode, the ``knobs`` the tuner and
the agent may change in that mode, ``scoring`` overrides and strategy
``options``. Entering a mode goes through ``TwoDService.apply`` (safety limits
apply as always); opening a camera never changes its mode.
"""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from camera_lab.core.config import ConfigError, load_yaml_model
from camera_lab.core.paths import SERIAL_RE, app_config_dir, safe_join, validate_serial
from camera_lab.two_d.models import ADVANCED_NAME_RE, CameraSettingsPatch
from camera_lab.two_d.scoring import ScoringParamsOverride

PROFILES_DIRNAME = "camera_profiles"
MODE_RE = re.compile(r"[a-z0-9_-]{1,40}")
#: Core setting names usable as knobs (advanced names are also allowed).
CORE_KNOBS = frozenset(
    {
        "exposure_us",
        "gain_db",
        "gamma",
        "white_balance",
        "pixel_format",
        "roi",
        "frame_rate_hz",
        "trigger_mode",
    }
)


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class Characteristics(_Frozen):
    """What the camera is (used for selection and comparison)."""

    shutter: Literal["rolling", "global", "unknown"] = "unknown"
    colour: Literal["bayer", "prism", "mono", "unknown"] = "unknown"
    hdr_capable: bool = False
    black_level: float | None = Field(default=None, ge=0, le=0.5)
    notes: tuple[str, ...] = ()


class ModeProfile(_Frozen):
    """One operating mode of a camera."""

    strategy: str = Field(pattern=r"^[a-z0-9_-]{1,40}$")
    description: str = Field(default="", max_length=1000)
    settings: CameraSettingsPatch = Field(default_factory=CameraSettingsPatch)
    knobs: tuple[str, ...] = Field(min_length=1)
    scoring: ScoringParamsOverride = Field(default_factory=ScoringParamsOverride)
    options: dict[str, Any] = Field(default_factory=dict)

    @field_validator("knobs")
    @classmethod
    def _knob_names(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        bad = [k for k in v if k not in CORE_KNOBS and ADVANCED_NAME_RE.fullmatch(k) is None]
        if bad:
            raise ValueError(f"unknown knobs: {', '.join(bad)}")
        return v


class WorkingRange(_Frozen):
    """Preferred range of one setting: guidance for tuning, never a limit."""

    min: float | None = None
    max: float | None = None

    @model_validator(mode="after")
    def _ordered(self) -> WorkingRange:
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError(f"min {self.min} > max {self.max}")
        return self


class FpLight(_Frozen):
    """A bench light state for the fingerprint: the dimmer setting and the iris f-number."""

    dimmer: int = Field(ge=0, le=100)
    f_number: float = Field(gt=0, le=64)


class FpChannelStage(FpLight):
    """HDR channels measured at one light state (one fingerprint step row)."""

    channels: tuple[str, ...] = Field(min_length=1, max_length=8)

    @field_validator("channels")
    @classmethod
    def _channel_names(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        bad = [c for c in v if re.fullmatch(r"[A-Za-z0-9_]{1,40}", c) is None]
        if bad:
            raise ValueError(f"invalid channel names: {', '.join(bad)}")
        return v


class FingerprintRecipe(_Frozen):
    """How the human sets the bench for this camera model's fingerprint (Phase 11 strict
    procedure, Derek 2026-10-22). Lens names refer to ``config/fingerprint_bench.yaml``."""

    lens: str = Field(pattern=r"^[a-z0-9_]{1,40}$")
    reference: FpLight
    channel_stages: tuple[FpChannelStage, ...] = Field(default=(), max_length=4)
    notes: tuple[str, ...] = ()


#: Used when a profile gives no working range: the old safety caps, now guidance
#: (limits policy, Derek 2026-10-21). Names ending in ``_us`` / ``_db`` (e.g.
#: ``hdr.ch0.exposure_us``) fall back to ``exposure_us`` / ``gain_db``.
DEFAULT_WORKING_RANGE: dict[str, WorkingRange] = {
    "exposure_us": WorkingRange(max=200_000),
    "gain_db": WorkingRange(min=0, max=18),
}
WORKING_NAME_RE = re.compile(r"[a-z0-9_.*?]{1,64}")


class CameraProfile(_Frozen):
    """``config/camera_profiles/<serial>.yaml``."""

    schema_version: Literal[1] = 1
    serial: str = Field(pattern=f"^{SERIAL_RE.pattern}$")
    label: str = Field(default="", max_length=120)
    characteristics: Characteristics = Field(default_factory=Characteristics)
    default_mode: str
    modes: dict[str, ModeProfile] = Field(min_length=1)
    #: Preferred ranges (guidance for the tuner / setup / agent), by setting name;
    #: shell wildcards allowed (e.g. ``hdr.ch*.gain_db``). Never blocks a write.
    working_range: dict[str, WorkingRange] = Field(
        default_factory=lambda: dict(DEFAULT_WORKING_RANGE)
    )
    #: Bench recipe for the fingerprint (None: discovery mode).
    fingerprint: FingerprintRecipe | None = None

    @field_validator("working_range")
    @classmethod
    def _working_names(cls, v: dict[str, WorkingRange]) -> dict[str, WorkingRange]:
        bad = [k for k in v if WORKING_NAME_RE.fullmatch(k) is None]
        if bad:
            raise ValueError(f"invalid working_range names: {', '.join(bad)}")
        return v

    def working(self, name: str) -> WorkingRange:
        """Working range of ``name``: exact, then wildcard, then by unit, else unbounded."""
        if name in self.working_range:
            return self.working_range[name]
        for key in sorted(self.working_range):
            if fnmatch.fnmatchcase(name, key):
                return self.working_range[key]
        unit = (
            "exposure_us" if name.endswith("_us") else "gain_db" if name.endswith("_db") else None
        )
        if unit is not None and unit in self.working_range:
            return self.working_range[unit]
        return WorkingRange()

    @model_validator(mode="after")
    def _modes_ok(self) -> CameraProfile:
        bad = [m for m in self.modes if MODE_RE.fullmatch(m) is None]
        if bad:
            raise ValueError(f"invalid mode names: {', '.join(bad)}")
        if self.default_mode not in self.modes:
            raise ValueError(f"default_mode {self.default_mode!r} is not one of the modes")
        return self

    def mode(self, name: str | None = None) -> tuple[str, ModeProfile]:
        """Return ``(name, mode)``; ``None`` means the default mode."""
        key = name or self.default_mode
        if key not in self.modes:
            raise KeyError(
                f"camera {self.serial} has no mode {key!r}; modes: {', '.join(self.modes)}"
            )
        return key, self.modes[key]


def generic_profile(serial: str) -> CameraProfile:
    """The profile used for a camera without a profile file."""
    return CameraProfile(
        serial=serial,
        label="generic (no profile file)",
        default_mode="linear",
        modes={
            "linear": ModeProfile(
                strategy="linear",
                description="Generic exposure → gain → grey-world white balance.",
                knobs=("exposure_us", "gain_db", "white_balance"),
            )
        },
    )


def profiles_dir() -> Path:
    """``APP/config/camera_profiles``."""
    return app_config_dir() / PROFILES_DIRNAME


def load_profile(serial: str, root: Path | None = None) -> CameraProfile:
    """Load the profile of ``serial`` (generic if there is no file).

    Raises:
        ConfigError: the file exists but is invalid, or its serial does not match.
    """
    validate_serial(serial)
    base = root if root is not None else profiles_dir()
    path = safe_join(base, f"{serial}.yaml")
    if not path.exists():
        return generic_profile(serial)
    profile = load_yaml_model(path, CameraProfile)
    if profile.serial != serial:
        raise ConfigError(path, f"serial {profile.serial!r} does not match file name {serial!r}")
    return profile
