"""Deterministic, non-AI image scores.

Pipeline: Bayer data is demosaiced first (scores are computed on RGB); the
optional ROI from ``scoring.yaml`` (fractions of the image) is cropped; luma
is Rec. 709 of the 0–1 normalised image.

Metrics (registered in :data:`METRICS`, reported in this order):

- ``mean_level``, ``p01``, ``p50``, ``p99``: mean and percentiles of luma (0–1).
- ``clipped_pct``: % of pixels where **any** channel equals the max code.
- ``black_pct``: % of pixels where **all** channels are ≤ ``black_threshold`` × full scale.
- ``sharpness``: variance of ``cv2.Laplacian(luma_u8, CV_64F, ksize=3)`` on the
  8-bit-scaled luma. Resolution- and scene-dependent: compare it only within
  one camera + ROI + scene. Not part of the composite.
- ``noise_sigma``: Immerkær (1996) fast noise estimate on float luma, kernel
  ``[[1,-2,1],[-2,4,-2],[1,-2,1]]``:
  ``σ = sqrt(π/2) · Σ|I∗N| / (6 (W−2)(H−2))``, in 0–1 units.
- ``histogram`` (stored separately): 256 bins of luma over [0, 1], as fractions.

Composite (0–100) = 100 × exposure × clipping × black × noise, each in [0, 1]:

- exposure = ``1 − min(1, |mean_level − target_mean| / tolerance)``
- clipping = ``max(0, 1 − clipped_pct / clip_max_pct)``
- black    = ``max(0, 1 − black_pct / black_max_pct)``
- noise    = ``max(0, 1 − noise_sigma / noise_max)``

Defaults: target_mean 0.45, tolerance 0.25, clip_max_pct 1.0, black_max_pct 5.0,
noise_max 0.03, black_threshold 0.02. All come from ``APP/config/scoring.yaml``,
optionally overridden per camera serial and per preset tag (tag wins).

Every float in the :class:`ScoreReport` is rounded to 6 decimals, so the same
image always gives byte-identical JSON.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import cv2
import numpy as np
import numpy.typing as npt
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from camera_lab.core.config import load_yaml_model
from camera_lab.core.paths import SERIAL_RE, app_config_dir
from camera_lab.core.registry import Registry
from camera_lab.two_d.models import PIXEL_FORMAT_BITS, Frame, MetricResult, ScoreReport
from camera_lab.two_d.processing import demosaic, to_float01, to_luma

SCORING_CONFIG_FILENAME = "scoring.yaml"
HISTOGRAM_BINS = 256
_IMMERKAER = np.array([[1, -2, 1], [-2, 4, -2], [1, -2, 1]], dtype=np.float64)


# ===================================================================== config
class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class FractionalROI(_Frozen):
    """Scoring ROI as fractions (0–1) of the image, so it survives camera ROI changes."""

    x: float = Field(ge=0, lt=1)
    y: float = Field(ge=0, lt=1)
    width: float = Field(gt=0, le=1)
    height: float = Field(gt=0, le=1)

    @model_validator(mode="after")
    def _inside(self) -> FractionalROI:
        if self.x + self.width > 1 + 1e-9 or self.y + self.height > 1 + 1e-9:
            raise ValueError("roi must lie inside the image (x+width <= 1, y+height <= 1)")
        return self


class ScoringParams(_Frozen):
    """Targets and thresholds for one scoring run."""

    target_mean: float = Field(default=0.45, gt=0, lt=1)
    #: what "brightness" means for the exposure score and the tuner: the mean level, or the
    #: highlights (99th percentile: "expose to the right" for linear / HDR data)
    level_metric: Literal["mean_level", "p99"] = "mean_level"
    tolerance: float = Field(default=0.25, gt=0, le=1)
    mean_pass_band: float = Field(default=0.05, gt=0, le=1)
    clip_max_pct: float = Field(default=1.0, gt=0, le=100)
    black_max_pct: float = Field(default=5.0, gt=0, le=100)
    noise_max: float = Field(default=0.03, gt=0, le=1)
    black_threshold: float = Field(default=0.02, ge=0, lt=1)
    roi: FractionalROI | None = None


class ScoringParamsOverride(_Frozen):
    """Partial override (per camera or per tag); unset fields keep the base value."""

    target_mean: float | None = Field(default=None, gt=0, lt=1)
    level_metric: Literal["mean_level", "p99"] | None = None
    tolerance: float | None = Field(default=None, gt=0, le=1)
    mean_pass_band: float | None = Field(default=None, gt=0, le=1)
    clip_max_pct: float | None = Field(default=None, gt=0, le=100)
    black_max_pct: float | None = Field(default=None, gt=0, le=100)
    noise_max: float | None = Field(default=None, gt=0, le=1)
    black_threshold: float | None = Field(default=None, ge=0, lt=1)
    roi: FractionalROI | None = None


class ScoringConfig(_Frozen):
    """``APP/config/scoring.yaml``."""

    schema_version: int = Field(default=1, ge=1, le=1)
    default: ScoringParams = Field(default_factory=ScoringParams)
    cameras: dict[str, ScoringParamsOverride] = Field(default_factory=dict)
    tags: dict[str, ScoringParamsOverride] = Field(default_factory=dict)

    @field_validator("cameras")
    @classmethod
    def _serial_keys(cls, v: dict[str, ScoringParamsOverride]) -> dict[str, ScoringParamsOverride]:
        bad = [k for k in v if SERIAL_RE.fullmatch(k) is None]
        if bad:
            raise ValueError(f"invalid camera serial keys: {', '.join(bad)}")
        return v

    def resolve(self, serial: str | None = None, tag: str | None = None) -> ScoringParams:
        """Return params: default ← cameras[serial] ← tags[tag]."""
        params = self.default
        for ovr in (self.cameras.get(serial or ""), self.tags.get(tag or "")):
            if ovr is not None:
                params = params.model_copy(update=ovr.model_dump(exclude_none=True))
        return ScoringParams.model_validate(params.model_dump())


def load_scoring_config(path: Path | None = None) -> ScoringConfig:
    """Load ``APP/config/scoring.yaml`` (or ``path``)."""
    return load_yaml_model(path or app_config_dir() / SCORING_CONFIG_FILENAME, ScoringConfig)


# ==================================================================== metrics
@dataclass(frozen=True)
class ScoringContext:
    """Inputs shared by all metrics (already demosaiced and ROI-cropped)."""

    codes: npt.NDArray[Any]  # integer codes, HxW or HxWx3
    luma: npt.NDArray[np.float64]  # 0–1
    full_scale: int
    params: ScoringParams


MetricFn = Callable[[ScoringContext], MetricResult]
METRICS: Registry[MetricFn] = Registry("metric")


def _r6(x: float) -> float:
    v = round(float(x), 6)
    return 0.0 if v == 0 else v


def _pct(mask: npt.NDArray[np.bool_]) -> float:
    return 100.0 * float(np.count_nonzero(mask)) / mask.size


@METRICS.register("mean_level")
def mean_level(ctx: ScoringContext) -> MetricResult:
    """Mean luma (0–1); passes within ``mean_pass_band`` of ``target_mean``."""
    v = float(ctx.luma.mean())
    ok = abs(v - ctx.params.target_mean) <= ctx.params.mean_pass_band
    return MetricResult(name="mean_level", value=_r6(v), passed=ok)


def _percentile(name: str, q: float) -> MetricFn:
    def fn(ctx: ScoringContext) -> MetricResult:
        return MetricResult(name=name, value=_r6(float(np.percentile(ctx.luma, q))))

    fn.__doc__ = f"{q}th percentile of luma (0–1)."
    return fn


METRICS.register("p01")(_percentile("p01", 1))
METRICS.register("p50")(_percentile("p50", 50))
METRICS.register("p99")(_percentile("p99", 99))


@METRICS.register("clipped_pct")
def clipped_pct(ctx: ScoringContext) -> MetricResult:
    """% of pixels with any channel at the maximum code."""
    hit = ctx.codes >= ctx.full_scale
    if hit.ndim == 3:
        hit = hit.any(axis=2)
    v = _pct(hit)
    return MetricResult(
        name="clipped_pct", value=_r6(v), unit="%", passed=v <= ctx.params.clip_max_pct
    )


@METRICS.register("black_pct")
def black_pct(ctx: ScoringContext) -> MetricResult:
    """% of pixels with all channels ≤ ``black_threshold`` × full scale."""
    dark = ctx.codes <= ctx.params.black_threshold * ctx.full_scale
    if dark.ndim == 3:
        dark = dark.all(axis=2)
    v = _pct(dark)
    return MetricResult(
        name="black_pct", value=_r6(v), unit="%", passed=v <= ctx.params.black_max_pct
    )


@METRICS.register("sharpness")
def sharpness(ctx: ScoringContext) -> MetricResult:
    """Variance of the 3×3 Laplacian of 8-bit luma (scene/resolution dependent)."""
    luma_u8 = np.rint(np.clip(ctx.luma, 0, 1) * 255).astype(np.uint8)
    lap = cv2.Laplacian(luma_u8, cv2.CV_64F, ksize=3)
    return MetricResult(name="sharpness", value=_r6(lap.var()))


@METRICS.register("noise_sigma")
def noise_sigma(ctx: ScoringContext) -> MetricResult:
    """Immerkær (1996) fast noise estimate of luma, 0–1 units."""
    v = immerkaer_sigma(ctx.luma)
    return MetricResult(name="noise_sigma", value=_r6(v), passed=v <= ctx.params.noise_max)


def immerkaer_sigma(img: npt.NDArray[np.float64]) -> float:
    """``sqrt(π/2) · Σ|I∗N| / (6 (W−2)(H−2))`` over the valid (unpadded) region."""
    h, w = img.shape
    if h < 3 or w < 3:
        raise ValueError("noise estimate needs an image of at least 3x3")
    conv = cv2.filter2D(img, cv2.CV_64F, _IMMERKAER, borderType=cv2.BORDER_REFLECT)[1:-1, 1:-1]
    return math.sqrt(math.pi / 2) * float(np.abs(conv).sum()) / (6.0 * (w - 2) * (h - 2))


def histogram(luma: npt.NDArray[np.float64]) -> tuple[float, ...]:
    """256-bin luma histogram over [0, 1], as fractions of the pixel count."""
    counts, _ = np.histogram(luma, bins=HISTOGRAM_BINS, range=(0.0, 1.0))
    return tuple(_r6(c / luma.size) for c in counts)


def sub_scores(metrics: Mapping[str, float], p: ScoringParams) -> dict[str, float]:
    """The four composite factors, each in [0, 1]."""
    return {
        "exposure": 1.0 - min(1.0, abs(metrics[p.level_metric] - p.target_mean) / p.tolerance),
        "clipping": max(0.0, 1.0 - metrics["clipped_pct"] / p.clip_max_pct),
        "black": max(0.0, 1.0 - metrics["black_pct"] / p.black_max_pct),
        "noise": max(0.0, 1.0 - metrics["noise_sigma"] / p.noise_max),
    }


# ==================================================================== scoring
def crop_roi(arr: npt.NDArray[Any], roi: FractionalROI | None) -> npt.NDArray[Any]:
    """Crop ``arr`` to a fractional ROI (``None`` = whole image)."""
    if roi is None:
        return arr
    h, w = arr.shape[:2]
    x0, y0 = round(roi.x * w), round(roi.y * h)
    x1, y1 = round((roi.x + roi.width) * w), round((roi.y + roi.height) * h)
    return arr[y0:y1, x0:x1]


def score_array(
    data: npt.NDArray[Any],
    pixel_format: str,
    bit_depth: int | None = None,
    params: ScoringParams | None = None,
) -> ScoreReport:
    """Score raw integer data (as delivered by the camera / stored in the TIFF)."""
    p = params or ScoringParams()
    if not isinstance(data, np.ndarray) or data.dtype not in (np.uint8, np.uint16, np.uint32):
        raise TypeError("data must be a uint8, uint16 or uint32 numpy array")
    bits = bit_depth if bit_depth is not None else PIXEL_FORMAT_BITS.get(pixel_format)
    if bits is None or not 1 <= bits <= 24:
        raise ValueError(f"unknown bit depth for pixel format {pixel_format!r}")
    codes = crop_roi(demosaic(data, pixel_format), p.roi)
    if codes.shape[0] < 3 or codes.shape[1] < 3:
        raise ValueError(f"image (after ROI) is too small to score: {codes.shape}")
    full_scale = (1 << bits) - 1
    luma = to_luma(to_float01(codes, bits))
    ctx = ScoringContext(codes=codes, luma=luma, full_scale=full_scale, params=p)
    metrics = tuple(METRICS.get(name)(ctx) for name in _metric_order())
    values = {m.name: m.value for m in metrics}
    subs = sub_scores(values, p)
    composite = 100.0 * math.prod(subs.values())
    return ScoreReport(
        metrics=metrics,
        histogram=histogram(luma),
        sub_scores={k: _r6(v) for k, v in subs.items()},
        composite=_r6(composite),
        params=p.model_dump(mode="json"),
    )


def score_frame(frame: Frame, params: ScoringParams | None = None) -> ScoreReport:
    """Score a :class:`Frame`."""
    return score_array(frame.data, frame.pixel_format, frame.bit_depth, params)


_BUILTIN_ORDER = (
    "mean_level",
    "p01",
    "p50",
    "p99",
    "clipped_pct",
    "black_pct",
    "sharpness",
    "noise_sigma",
)


def _metric_order() -> list[str]:
    """Built-ins first (fixed order), then any plugin metrics alphabetically."""
    extra = [n for n in METRICS.names() if n not in _BUILTIN_ORDER]
    return [*_BUILTIN_ORDER, *extra]
