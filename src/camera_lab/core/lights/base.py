"""Light-controller interface, channel model, config model and driver registry.

Drivers know nothing about safety limits: :class:`~camera_lab.core.lights.service.LightService`
applies max %, step ramping and rate limits on top of any controller.

Driver rules:
- ``set_levels`` sets only the given channels and validates against the
  channel's hardware ``max_pct`` (it raises ``ValueError`` on bad input).
- ``all_off`` must be safe to call at any time, including from a signal
  handler while another call is in progress (use an ``RLock``), and more than once.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from camera_lab.core.registry import Registry
from camera_lab.core.safety import CHANNEL_ID_RE


class LightChannel(BaseModel):
    """One controllable light channel."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    id: str = Field(pattern=f"^{CHANNEL_ID_RE}$")
    name: str = Field(min_length=1, max_length=80)
    colour: str = Field(default="", max_length=80, description="colour / wavelength text")
    max_pct: float = Field(default=100.0, gt=0, le=100, description="hardware maximum, %")


class LightChannelConfig(LightChannel):
    """A channel entry in ``lights.yaml`` (driver-specific extras allowed)."""

    sim_weight: float = Field(default=1.0, ge=0, le=10, description="simulated light only")


class LightsConfig(BaseModel):
    """``APP/config/lights.yaml``."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    schema_version: int = Field(default=1, ge=1, le=1)
    driver: str = Field(pattern=r"^[a-z0-9_-]{1,64}$")
    channels: tuple[LightChannelConfig, ...] = Field(min_length=1, max_length=64)
    options: Mapping[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _unique_ids(self) -> LightsConfig:
        ids = [c.id for c in self.channels]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ValueError(f"duplicate channel ids: {', '.join(dupes)}")
        return self


class LightController(ABC):
    """Abstract light controller (simulated now; real LED drivers later)."""

    @classmethod
    @abstractmethod
    def from_config(cls, config: LightsConfig) -> Self:
        """Build the controller from ``lights.yaml``."""

    @abstractmethod
    def channels(self) -> list[LightChannel]:
        """Return the channels this controller drives."""

    @abstractmethod
    def get_levels(self) -> dict[str, float]:
        """Return the current level (%) of every channel."""

    @abstractmethod
    def set_levels(self, levels: Mapping[str, float]) -> None:
        """Set the given channels (%). Unlisted channels are unchanged."""

    @abstractmethod
    def all_off(self) -> None:
        """Turn every channel off immediately. Must be idempotent and signal-safe."""

    @abstractmethod
    def close(self) -> None:
        """Turn everything off and release the hardware."""


#: Light drivers, selected by ``lights.yaml: driver``.
LIGHT_DRIVERS: Registry[type[LightController]] = Registry("light driver")
