"""Light control: interface, simulated driver, safety service, config loading.

``APP/config/lights.yaml`` selects the driver class by name through
:data:`LIGHT_DRIVERS`. Adding real hardware = one new module that registers
itself + a config change; nothing else is edited.
"""

from __future__ import annotations

from pathlib import Path

from camera_lab.core.config import load_yaml_model
from camera_lab.core.lights import simulated as _simulated  # noqa: F401 - registers "simulated"
from camera_lab.core.lights.base import (
    LIGHT_DRIVERS,
    LightChannel,
    LightChannelConfig,
    LightController,
    LightsConfig,
)
from camera_lab.core.lights.service import (
    LightChannelResult,
    LightService,
    LightSetResult,
    install_lights_off_handlers,
)
from camera_lab.core.lights.simulated import DEFAULT_SIM_CHANNELS, SimulatedLight
from camera_lab.core.paths import app_config_dir

LIGHTS_CONFIG_FILENAME = "lights.yaml"

__all__ = [
    "DEFAULT_SIM_CHANNELS",
    "LIGHT_DRIVERS",
    "LightChannel",
    "LightChannelConfig",
    "LightChannelResult",
    "LightController",
    "LightService",
    "LightSetResult",
    "LightsConfig",
    "SimulatedLight",
    "create_light_controller",
    "install_lights_off_handlers",
    "load_lights_config",
]


def load_lights_config(path: Path | None = None) -> LightsConfig:
    """Load ``APP/config/lights.yaml`` (or ``path``)."""
    return load_yaml_model(path or app_config_dir() / LIGHTS_CONFIG_FILENAME, LightsConfig)


def create_light_controller(config: LightsConfig) -> LightController:
    """Instantiate the driver named by ``config.driver``."""
    return LIGHT_DRIVERS.get(config.driver).from_config(config)
