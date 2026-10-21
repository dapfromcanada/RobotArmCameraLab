"""Camera presets: named settings + light levels, consumed by the robot stack.

Path: ``WS/config/camera_presets/two_d/<camera_serial>/<name>.yaml`` (git-tracked).

- Saving never overwrites unless ``overwrite=True`` (CLI ``--force``); the old
  file is then kept as ``<name>.yaml.bak-<UTC timestamp>``.
- Writes are atomic. Every path goes through :func:`safe_join`; serials and
  names are validated. Loading uses ``yaml.safe_load`` + pydantic and checks
  that the file's ``name`` and ``camera.serial`` match its path.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from camera_lab.core.config import ConfigError, load_yaml_model
from camera_lab.core.logging import get_logger
from camera_lab.core.paths import (
    PRESET_NAME_RE,
    SERIAL_RE,
    safe_join,
    two_d_presets_root,
    validate_preset_name,
    validate_serial,
)
from camera_lab.core.records import CAPTURE_ID_RE, _as_utc, atomic_write_bytes, utc_now

_log = get_logger(__name__)


class PresetExistsError(FileExistsError):
    """A preset with that name exists and ``overwrite`` was not set."""


class PresetNotFoundError(FileNotFoundError):
    """No preset with that name for that camera."""


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class PresetCamera(_Model):
    """Camera identity stored in a preset."""

    vendor: str = Field(max_length=80)
    model: str = Field(max_length=80)
    serial: str = Field(pattern=f"^{SERIAL_RE.pattern}$")
    firmware: str | None = Field(default=None, max_length=80)


class PresetTags(_Model):
    """Free-form tags: what object / lighting setup the preset is for, plus notes."""

    object: str | None = Field(default=None, max_length=200)
    lighting_setup: str | None = Field(default=None, max_length=200)
    notes: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("notes")
    @classmethod
    def _note_length(cls, v: list[str]) -> list[str]:
        if any(len(n) > 500 for n in v):
            raise ValueError("each note must be <= 500 characters")
        return v


class Preset(_Model):
    """One preset YAML file."""

    schema_version: Literal[1] = 1
    name: str = Field(pattern=f"^{PRESET_NAME_RE.pattern}$")
    camera: PresetCamera
    settings: dict[str, Any]
    lights: dict[str, float] = Field(default_factory=dict)
    tags: PresetTags = Field(default_factory=PresetTags)
    source_capture_id: str | None = Field(default=None, pattern=f"^{CAPTURE_ID_RE.pattern}$")
    scores_at_save: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    created_by: Literal["human", "agent"]

    @field_validator("created_at")
    @classmethod
    def _utc(cls, v: datetime) -> datetime:
        return _as_utc(v)

    @field_validator("lights")
    @classmethod
    def _light_range(cls, v: dict[str, float]) -> dict[str, float]:
        bad = [k for k, pct in v.items() if not 0.0 <= pct <= 100.0]
        if bad:
            raise ValueError(f"light levels must be 0..100 %: {', '.join(bad)}")
        return v


@dataclass(frozen=True)
class PresetRef:
    """A preset found on disk."""

    serial: str
    name: str
    path: Path


class PresetStore:
    """Save / load / list presets under ``WS/config/camera_presets/two_d``."""

    def __init__(self, root: Path | None = None, *, now: Callable[[], datetime] = utc_now) -> None:
        self.root = Path(root) if root is not None else two_d_presets_root()
        self._now = now

    def path(self, serial: str, name: str) -> Path:
        """Return the validated path of ``<serial>/<name>.yaml``."""
        return safe_join(self.root, validate_serial(serial), f"{validate_preset_name(name)}.yaml")

    def save(self, preset: Preset, *, overwrite: bool = False) -> Path:
        """Write ``preset``; return its path.

        Raises:
            PresetExistsError: the preset exists and ``overwrite`` is False.
        """
        preset = Preset.model_validate(preset.model_dump())  # re-validate a possibly mutated copy
        path = self.path(preset.camera.serial, preset.name)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = yaml.safe_dump(preset.model_dump(mode="json"), sort_keys=False, allow_unicode=True)
        if path.exists():
            if not overwrite:
                raise PresetExistsError(
                    f"preset {preset.name!r} already exists for {preset.camera.serial}; "
                    "use overwrite (CLI --force) to replace it"
                )
            stamp = self._now().strftime("%Y%m%dT%H%M%S%fZ")
            bak = safe_join(path.parent, f"{path.name}.bak-{stamp}")
            os.link(path, bak)  # keep the old file; the new one replaces `path` atomically
            _log.info("preset_backup", path=str(bak))
        atomic_write_bytes(path, data.encode("utf-8"))
        _log.info("preset_saved", path=str(path))
        return path

    def load(self, serial: str, name: str) -> Preset:
        """Load and validate one preset.

        Raises:
            PresetNotFoundError: no such preset.
            ConfigError: the file is invalid or its name/serial do not match its path.
        """
        path = self.path(serial, name)
        if not path.is_file():
            raise PresetNotFoundError(f"no preset {name!r} for camera {serial}")
        preset = load_yaml_model(path, Preset)
        if preset.name != name or preset.camera.serial != serial:
            raise ConfigError(
                path,
                f"name/serial in file ({preset.name}, {preset.camera.serial})"
                f" do not match its path ({name}, {serial})",
            )
        return preset

    def list(self, serial: str | None = None) -> list[PresetRef]:
        """List presets (all cameras, or one), sorted by serial then name."""
        if not self.root.is_dir():
            return []
        if serial is not None:
            serial_dirs = [safe_join(self.root, validate_serial(serial))]
        else:
            serial_dirs = [d for d in self.root.iterdir() if SERIAL_RE.fullmatch(d.name)]
        refs: list[PresetRef] = []
        for d in serial_dirs:
            if not d.is_dir() or d.is_symlink():
                continue
            for f in d.glob("*.yaml"):
                if f.is_file() and not f.is_symlink() and PRESET_NAME_RE.fullmatch(f.stem):
                    refs.append(PresetRef(d.name, f.stem, f))
        return sorted(refs, key=lambda r: (r.serial, r.name))

    def new(self, **fields: Any) -> Preset:
        """Build a :class:`Preset` with ``created_at`` set to now (UTC)."""
        fields.setdefault("created_at", self._now())
        return Preset.model_validate(fields)
