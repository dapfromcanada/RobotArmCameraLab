"""Simulated light: levels kept in memory, shared in-process with the simulated camera.

The simulated camera multiplies scene brightness by :meth:`SimulatedLight.light_factor`:
``sum(level_pct / 100 * sim_weight)`` over all channels.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping, Sequence
from typing import Self

from camera_lab.core.lights.base import (
    LIGHT_DRIVERS,
    LightChannel,
    LightChannelConfig,
    LightController,
    LightsConfig,
)
from camera_lab.core.logging import get_logger

_log = get_logger(__name__)

#: Used when no lights.yaml is given (e.g. ``--sim`` without config).
DEFAULT_SIM_CHANNELS: tuple[LightChannelConfig, ...] = (
    LightChannelConfig(id="white_ring", name="White ring light", colour="white", sim_weight=0.5),
    LightChannelConfig(id="white_bar", name="White bar light", colour="white", sim_weight=0.3),
    LightChannelConfig(id="red", name="Red", colour="red ~625 nm", sim_weight=0.1),
    LightChannelConfig(id="blue", name="Blue", colour="blue ~470 nm", sim_weight=0.1),
)


@LIGHT_DRIVERS.register("simulated")
class SimulatedLight(LightController):
    """In-memory light controller with per-channel brightness weights."""

    def __init__(self, channels: Sequence[LightChannelConfig] = DEFAULT_SIM_CHANNELS) -> None:
        if not channels:
            raise ValueError("SimulatedLight needs at least one channel")
        self._channels = {c.id: c for c in channels}
        if len(self._channels) != len(channels):
            raise ValueError("duplicate channel ids")
        self._levels = dict.fromkeys(self._channels, 0.0)
        self._lock = threading.RLock()  # RLock: all_off may run from a signal handler
        self.history: list[dict[str, float]] = []
        self.off_count = 0

    @classmethod
    def from_config(cls, config: LightsConfig) -> Self:
        """Build from ``lights.yaml`` channels."""
        return cls(config.channels)

    def channels(self) -> list[LightChannel]:
        """Return the channels (without simulation weights)."""
        return [
            LightChannel(**c.model_dump(exclude={"sim_weight"})) for c in self._channels.values()
        ]

    def get_levels(self) -> dict[str, float]:
        """Return a copy of the current levels."""
        with self._lock:
            return dict(self._levels)

    def set_levels(self, levels: Mapping[str, float]) -> None:
        """Set channel levels (hardware-range validated; safety is the service's job)."""
        with self._lock:
            for cid, pct in levels.items():
                if cid not in self._channels:
                    raise ValueError(f"unknown light channel {cid!r}")
                if not 0.0 <= float(pct) <= self._channels[cid].max_pct:
                    raise ValueError(f"{cid} level {pct} outside 0..{self._channels[cid].max_pct}")
            for cid, pct in levels.items():
                self._levels[cid] = float(pct)
            self.history.append(dict(self._levels))

    def all_off(self) -> None:
        """Set every channel to 0 and log it."""
        with self._lock:
            self._levels = dict.fromkeys(self._channels, 0.0)
            self.off_count += 1
        _log.info("lights_all_off", driver="simulated")

    def close(self) -> None:
        """Turn everything off."""
        self.all_off()

    def light_factor(self) -> float:
        """Return ``sum(level_pct / 100 * sim_weight)`` for the simulated camera."""
        with self._lock:
            return sum(
                self._levels[cid] / 100.0 * ch.sim_weight for cid, ch in self._channels.items()
            )
