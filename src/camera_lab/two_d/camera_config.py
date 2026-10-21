"""``APP/config/cameras.yaml``: human aliases and per-camera driver options.

Aliases (``triton: "233100505"``) are resolved to serials at the boundary
(CLI/MCP/GUI); everything inside camera_lab uses the serial as ``camera_id``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from camera_lab.core.config import load_yaml_model
from camera_lab.core.paths import SERIAL_RE, app_config_dir

CAMERAS_CONFIG_FILENAME = "cameras.yaml"
ALIAS_RE = r"[a-z0-9_-]{1,40}"


class CamerasConfig(BaseModel):
    """Aliases and per-serial driver options (network/stream overrides)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = Field(default=1, ge=1, le=1)
    aliases: dict[str, str] = Field(default_factory=dict)
    cameras: dict[str, dict[str, Any]] = Field(default_factory=dict)

    @field_validator("aliases")
    @classmethod
    def _aliases(cls, v: dict[str, str]) -> dict[str, str]:
        import re

        for alias, serial in v.items():
            if re.fullmatch(ALIAS_RE, alias) is None:
                raise ValueError(f"invalid alias {alias!r}: must match {ALIAS_RE}")
            if SERIAL_RE.fullmatch(serial) is None:
                raise ValueError(f"alias {alias!r}: invalid serial {serial!r}")
        return v

    @field_validator("cameras")
    @classmethod
    def _serials(cls, v: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        bad = [k for k in v if SERIAL_RE.fullmatch(k) is None]
        if bad:
            raise ValueError(f"invalid camera serial keys: {', '.join(bad)}")
        return v

    def resolve(self, name: str) -> str:
        """Return the serial for an alias, or ``name`` itself."""
        return self.aliases.get(name, name)

    def options_for(self, serial: str) -> dict[str, Any]:
        """Driver options for ``serial`` (empty if none)."""
        return dict(self.cameras.get(serial, {}))


def load_cameras_config(path: Path | None = None) -> CamerasConfig:
    """Load ``APP/config/cameras.yaml``; a missing file means no aliases/options."""
    p = path or app_config_dir() / CAMERAS_CONFIG_FILENAME
    if not p.exists():
        return CamerasConfig()
    return load_yaml_model(p, CamerasConfig)
