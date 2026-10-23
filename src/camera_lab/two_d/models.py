"""Data models for 2D cameras: identity, settings, capabilities, frames, scores.

Units: exposure in µs (``exposure_us``), gain in dB (``gain_db``), frame rate in
Hz, image levels normalised 0–1 of the pixel format's full scale.

:class:`Frame` is the public frame type returned by ``TwoDService.grab()`` and
consumed by other apps (calibration_lab). Keep its fields stable.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any, Literal

import numpy as np
import numpy.typing as npt
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from camera_lab.core.paths import SERIAL_RE
from camera_lab.core.safety import CameraRange

WbChannel = Literal["red", "green", "blue"]
TriggerMode = Literal["off", "software"]
#: Advanced settings hold numbers or enum strings (booleans are "off"/"on" enums).
AdvancedValue = float | str
ADVANCED_NAME_RE = re.compile(r"[a-z0-9_]+(\.[a-z0-9_]+){1,4}")
SettingKind = Literal["float", "int", "enum"]

#: Pixel formats understood by processing/scoring, with their bit depth.
PIXEL_FORMAT_BITS: dict[str, int] = {
    "Mono8": 8,
    "Mono10": 10,
    "Mono12": 12,
    "Mono16": 16,
    "BayerRG8": 8,
    "BayerGR8": 8,
    "BayerGB8": 8,
    "BayerBG8": 8,
    "BayerRG10": 10,
    "BayerGR10": 10,
    "BayerGB10": 10,
    "BayerBG10": 10,
    "BayerRG12": 12,
    "BayerGR12": 12,
    "BayerGB12": 12,
    "BayerBG12": 12,
    "BayerRG16": 16,
    "BayerGR16": 16,
    "BayerGB16": 16,
    "BayerBG16": 16,
    "RGB8": 8,
    "RGB10": 10,
    "RGB12": 12,
    "RGB16": 16,
    # GenICam "p" formats are bit-packed on the wire; drivers unpack them to
    # uint16 holding the native 10/12-bit values.
    "Mono10p": 10,
    "Mono12p": 12,
    "BayerRG10p": 10,
    "BayerRG12p": 12,
    "BayerGR12p": 12,
    "BayerGB12p": 12,
    "BayerBG12p": 12,
    "RGB10p": 10,
    "RGB12p": 12,
    # JAI Apex (GigE Vision) RGB packings, unpacked by the eBUS driver.
    "RGB10p32": 10,
    "RGB10V1Packed": 10,
    "RGB12V1Packed": 12,
    # 24-bit linear HDR (Lucid TRI054S / IMX490, LUT off): 3 bytes per pixel on the
    # wire, unpacked to uint32.
    "Mono24": 24,
    "BayerRG24": 24,
}


def is_bayer(pixel_format: str) -> bool:
    """True for ``Bayer*`` formats."""
    return pixel_format.startswith("Bayer")


def is_rgb(pixel_format: str) -> bool:
    """True for ``RGB*`` formats."""
    return pixel_format.startswith("RGB")


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class CameraInfo(_Frozen):
    """Identity of one camera, as reported by discovery."""

    vendor: str = Field(max_length=80)
    model: str = Field(max_length=80)
    serial: str = Field(pattern=f"^{SERIAL_RE.pattern}$")
    firmware: str | None = Field(default=None, max_length=80)
    interface: str = Field(default="", max_length=40)
    ip: str | None = Field(default=None, max_length=64)
    driver: str = Field(max_length=40)

    @property
    def camera_id(self) -> str:
        """The camera id used everywhere (the serial)."""
        return self.serial


class ROI(_Frozen):
    """Region of interest in sensor pixels."""

    x: int = Field(ge=0)
    y: int = Field(ge=0)
    width: int = Field(gt=0)
    height: int = Field(gt=0)


class CameraSettings(_Frozen):
    """Full camera settings as read back from the camera.

    ``gamma``, ``white_balance`` and ``frame_rate_hz`` are ``None`` when the
    camera does not support (or has disabled) them.
    """

    exposure_us: float
    gain_db: float
    gamma: float | None = None
    white_balance: dict[WbChannel, float] | None = None
    pixel_format: str
    roi: ROI
    frame_rate_hz: float | None = None
    trigger_mode: TriggerMode = "off"
    #: Vendor/mode-specific settings (e.g. ``hdr.ch0.exposure_us``), as read back.
    advanced: dict[str, AdvancedValue] = Field(default_factory=dict)


class CameraSettingsPatch(_Frozen):
    """A partial settings change. Unset fields are left unchanged.

    ``white_balance`` may name only some channels.
    """

    exposure_us: float | None = None
    gain_db: float | None = None
    gamma: float | None = None
    white_balance: dict[WbChannel, float] | None = None
    pixel_format: str | None = Field(default=None, max_length=64)
    roi: ROI | None = None
    frame_rate_hz: float | None = None
    trigger_mode: TriggerMode | None = None
    #: Vendor/mode-specific settings; names come from ``capabilities()``.
    advanced: dict[str, AdvancedValue] | None = None

    @field_validator("advanced")
    @classmethod
    def _advanced_names(cls, v: dict[str, AdvancedValue] | None) -> dict[str, AdvancedValue] | None:
        if v is not None:
            bad = [k for k in v if ADVANCED_NAME_RE.fullmatch(k) is None]
            if bad:
                raise ValueError(f"invalid advanced setting names: {', '.join(bad)}")
        return v

    def changed_fields(self) -> list[str]:
        """Names of the fields set in this patch, in declaration order."""
        return [n for n in type(self).model_fields if getattr(self, n) is not None]


class SettingSpec(_Frozen):
    """What the camera allows for one setting, read live from the camera.

    Names: ``exposure_us``, ``gain_db``, ``gamma``, ``wb_red``, ``wb_green``,
    ``wb_blue``, ``pixel_format``, ``roi_x``, ``roi_y``, ``roi_width``,
    ``roi_height``, ``frame_rate_hz``, ``trigger_mode``. A setting the camera
    does not have is absent from ``capabilities()``.
    """

    name: str
    kind: SettingKind
    min: float | None = None
    max: float | None = None
    increment: float | None = None
    options: tuple[str, ...] = ()
    unit: str = ""
    affects_calibration: bool = False
    requires_stream_stop: bool = False

    @model_validator(mode="after")
    def _consistent(self) -> SettingSpec:
        if self.kind == "enum":
            if not self.options:
                raise ValueError(f"{self.name}: enum spec needs options")
        elif self.min is None or self.max is None or self.min > self.max:
            raise ValueError(f"{self.name}: numeric spec needs min <= max")
        return self

    def camera_range(self) -> CameraRange:
        """Return the numeric range for the safety layer (numeric kinds only)."""
        if self.min is None or self.max is None:
            raise ValueError(f"{self.name} is not numeric")
        return CameraRange(self.min, self.max, self.increment)


FieldStatus = Literal["applied", "clamped", "rejected"]


class FieldResult(_Frozen):
    """Outcome of one setting in an apply."""

    field: str
    status: FieldStatus
    requested: Any = None
    value: Any = None
    reason: str | None = None


class ApplyResult(_Frozen):
    """Outcome of ``Camera2D.apply`` / ``TwoDService.apply``."""

    results: tuple[FieldResult, ...] = ()
    settings: CameraSettings
    warnings: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        """True if no field was rejected."""
        return all(r.status != "rejected" for r in self.results)


class Frame(BaseModel):
    """One grabbed frame (a copy; the driver buffer is already released).

    ``data`` holds the raw values as delivered: Bayer stays Bayer (HxW), 10/12-bit
    data is unpacked to ``uint16`` with native values, RGB is HxWx3.
    ``settings`` are the settings at grab time, as last read back.
    """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    data: npt.NDArray[Any]
    pixel_format: str
    bit_depth: int = Field(ge=1, le=24)
    timestamp_utc: datetime
    frame_id: int = Field(ge=0)
    settings: CameraSettings

    @field_validator("data")
    @classmethod
    def _check_data(cls, v: Any) -> npt.NDArray[Any]:
        if not isinstance(v, np.ndarray):
            raise TypeError("data must be a numpy array")
        if v.dtype not in (np.uint8, np.uint16, np.uint32):
            raise ValueError(f"data dtype must be uint8, uint16 or uint32, got {v.dtype}")
        if not (v.ndim == 2 or (v.ndim == 3 and v.shape[2] == 3)) or v.size == 0:
            raise ValueError(f"data shape must be HxW or HxWx3, got {v.shape}")
        return v

    @field_validator("timestamp_utc")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("timestamp_utc must be timezone-aware")
        return v

    @property
    def width(self) -> int:
        """Image width in pixels."""
        return int(self.data.shape[1])

    @property
    def height(self) -> int:
        """Image height in pixels."""
        return int(self.data.shape[0])

    @property
    def full_scale(self) -> int:
        """Maximum code value for ``bit_depth``."""
        return (1 << self.bit_depth) - 1


class MetricResult(_Frozen):
    """One scalar score with an optional pass/fail against scoring.yaml."""

    name: str
    value: float
    unit: str = ""
    passed: bool | None = None


class ProcessorResult(_Frozen):
    """Output summary of an image processor (for the GUI plugin panel)."""

    name: str
    result: dict[str, Any] = Field(default_factory=dict)


class ScoreReport(_Frozen):
    """Deterministic image scores (floats rounded to 6 decimals).

    ``to_json()`` gives byte-identical output for identical input.
    """

    schema_version: Literal[1] = 1
    metrics: tuple[MetricResult, ...]
    histogram: tuple[float, ...]
    sub_scores: dict[str, float]
    composite: float
    params: dict[str, Any]

    def metric(self, name: str) -> float:
        """Return the value of metric ``name``."""
        for m in self.metrics:
            if m.name == name:
                return m.value
        raise KeyError(name)

    def to_json(self) -> bytes:
        """Canonical JSON: sorted keys, no whitespace."""
        return json.dumps(
            self.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        ).encode("ascii")
