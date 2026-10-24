"""Camera model sheets (``APP/config/camera_models/<model>.yaml``): the machine-readable twin
of each camera model's white paper, written from the bench fingerprint.

Only the parts camera_lab's tools rely on are validated; the rest of the sheet is
documentation and is kept but not interpreted. Loading uses ``yaml.safe_load`` with a size
limit (``core.config``).
"""

from __future__ import annotations

import bisect
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from camera_lab.core.config import load_yaml_model
from camera_lab.core.paths import app_config_dir, safe_join

MODELS_DIRNAME = "camera_models"
MODEL_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")


class _Sheet(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")


class SheetSensor(_Sheet):
    shutter: Literal["rolling", "global", "unknown"] = "unknown"
    colour: str = "unknown"


class SheetTiming(_Sheet):
    row_time_us: float | None = Field(default=None, gt=0, le=1000)
    #: full-frame free-run frame rate per pixel format, measured (the camera's own maximum
    #: can read low right after a reset: 6.56 vs 6.93 fps for BayerRG24, 2026-10-24)
    fps_full_frame: dict[str, float] = Field(default_factory=dict)


class SheetSnr(_Sheet):
    #: level (fraction of full scale) → SNR, measured on the bench
    points: dict[float, float] = Field(default_factory=dict)

    def at(self, level: float) -> float | None:
        """SNR expected at ``level`` (linear interpolation between measured points)."""
        if not self.points:
            return None
        xs = sorted(self.points)
        if level <= xs[0]:
            return self.points[xs[0]]
        if level >= xs[-1]:
            return self.points[xs[-1]]
        i = bisect.bisect_right(xs, level)
        x0, x1 = xs[i - 1], xs[i]
        y0, y1 = self.points[x0], self.points[x1]
        return y0 + (y1 - y0) * (level - x0) / (x1 - x0)

    def best_level(self) -> float | None:
        """The level with the best measured SNR."""
        return max(self.points, key=lambda k: self.points[k]) if self.points else None


class SheetConditions(_Sheet):
    """The settings the performance numbers were measured at (other values scale them)."""

    gain_db: float | None = None
    black_level_pct: float | None = None


class SheetStep(_Sheet):
    """One quantization regime: from this level (fraction of full scale) up, values step by
    ``step_dn`` (one code of the HDR channel in use)."""

    from_level: float = Field(ge=0, le=1)
    step_dn: float = Field(gt=0)


class SheetPerformance(_Sheet):
    conditions: SheetConditions = Field(default_factory=SheetConditions)
    quantization: tuple[SheetStep, ...] = ()
    full_scale_dn: float | None = None
    black_dn: float | None = None
    read_noise_dn: float | None = None
    row_noise_dn: float | None = None
    dynamic_range_stops: float | None = None
    snr_vs_signal: SheetSnr | None = None


class SheetLinearCheck(_Sheet):
    expect_ratio: tuple[float, float] = (1.95, 2.05)
    lut_on_ratio: float | None = None


class SheetStateChecks(_Sheet):
    linear_output: SheetLinearCheck = Field(default_factory=SheetLinearCheck)


class ModelSheet(_Sheet):
    model: str
    vendor: str = ""
    sensor: SheetSensor = Field(default_factory=SheetSensor)
    design_mode: str | None = None
    timing: SheetTiming = Field(default_factory=SheetTiming)
    performance: SheetPerformance = Field(default_factory=SheetPerformance)
    state_checks: SheetStateChecks = Field(default_factory=SheetStateChecks)


def models_dir() -> Path:
    """``APP/config/camera_models``."""
    return app_config_dir() / MODELS_DIRNAME


def load_model_sheet(model: str, root: Path | None = None) -> ModelSheet | None:
    """The sheet of camera ``model`` (e.g. ``TRI054S-C``), or None when there is none."""
    if MODEL_NAME_RE.fullmatch(model) is None:
        return None
    path = safe_join(root if root is not None else models_dir(), f"{model}.yaml")
    if not path.exists():
        return None
    return load_yaml_model(path, ModelSheet)
