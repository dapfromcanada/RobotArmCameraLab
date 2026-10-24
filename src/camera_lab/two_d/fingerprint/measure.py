"""Fingerprint measurements (Phase 11): pure numpy on raw frames, in DN.

Raw data only: a Bayer mosaic is measured on its first green plane (``[0::2,
1::2]``), an RGB (prism) image on its green channel, mono as is. Demosaicing
would interpolate and smooth the noise, biasing the photon transfer.

- **Temporal noise** of a pair: ``var(a − b) / 2`` (fixed patterns cancel).
- **Photon transfer** (EMVA 1288 style): ``σ²_t = K · (μ − black) + σ²_read`` on the
  linear part of a flat-field exposure sweep → system gain ``K`` (DN/e⁻), read
  noise (e⁻) and full well (e⁻) = (saturation − black) / K.
- **Darks** (cap on): black level, temporal read noise, dark signal
  non-uniformity (DSNU, spatial σ of the averaged dark minus its temporal
  part), dark current (slope of the black level over exposure) and hot pixels
  (robust outliers of a long averaged dark).
- **Flat** (even diffuser): vignetting (corner / centre ratio of the smoothed
  flat) and pixel response non-uniformity (PRNU, σ of flat / smoothed flat,
  temporal part removed).
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Sequence
from typing import Any

import cv2
import numpy as np
import numpy.typing as npt
from pydantic import BaseModel, ConfigDict

from camera_lab.two_d.models import is_bayer

#: Photon transfer fit range, as a fraction of (saturation − black).
PTC_LO = 0.02
PTC_HI = 0.70
#: Hot pixel: above median + this × robust σ in a long averaged dark.
HOT_SIGMA = 8.0
MAX_HOT_PIXELS = 1000
CLIP_SIGMA = 5.0  # PRNU: values beyond 5 robust σ are hot pixels / dust
CORNER_FRAC = 0.05  # corner / centre boxes for vignetting, fraction of the size
SHADING_BLUR_FRAC = 1 / 16  # Gaussian σ for the smooth shading, fraction of the size

Array = npt.NDArray[Any]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


#: Bayer pixel sites at (row, col) (0,0), (0,1), (1,0), (1,1) per pattern. Gr is the green
#: on the red rows, Gb the green on the blue rows.
BAYER_SITES: dict[str, tuple[str, str, str, str]] = {
    "BayerRG": ("R", "Gr", "Gb", "B"),
    "BayerGR": ("Gr", "R", "B", "Gb"),
    "BayerGB": ("Gb", "B", "R", "Gr"),
    "BayerBG": ("B", "Gb", "Gr", "R"),
}
_SITE_OFFSETS = ((0, 0), (0, 1), (1, 0), (1, 1))


def colour_planes(data: Array, pixel_format: str) -> dict[str, npt.NDArray[np.float64]]:
    """Each raw colour plane in DN (float64), un-interpolated: R, Gr, Gb, B for a Bayer
    mosaic; R, G, B for an RGB / BGR image; empty for mono."""
    if is_bayer(pixel_format):
        sites = BAYER_SITES[pixel_format[:7]]
        return {
            name: np.asarray(data[dy::2, dx::2], dtype=np.float64)
            for name, (dy, dx) in zip(sites, _SITE_OFFSETS, strict=True)
        }
    if data.ndim == 3:
        order = ("B", "G", "R") if pixel_format.startswith("BGR") else ("R", "G", "B")
        return {name: np.asarray(data[..., i], dtype=np.float64) for i, name in enumerate(order)}
    return {}


def raw_plane(data: Array, pixel_format: str) -> npt.NDArray[np.float64]:
    """One un-interpolated colour plane in DN (float64): the first green site of a Bayer
    mosaic, the green channel of RGB / BGR, mono as is."""
    if is_bayer(pixel_format):
        dy, dx = _SITE_OFFSETS[BAYER_SITES[pixel_format[:7]].index("Gr")]
        plane = data[dy::2, dx::2]
    elif data.ndim == 3:
        plane = data[..., 1]
    else:
        plane = data
    return np.asarray(plane, dtype=np.float64)


def centre(a: Array, frac: float = 0.5) -> Array:
    """The central ``frac`` × ``frac`` region."""
    h, w = a.shape[:2]
    dy, dx = int(h * (1 - frac) / 2), int(w * (1 - frac) / 2)
    return a[dy : h - dy, dx : w - dx]


def row_variance(a: Array, b: Array) -> float:
    """Row-correlated temporal variance (DN²) of two frames of the same scene: the variance of
    the row means of (a − b)/√2, less what uncorrelated pixel noise puts there. A rolling
    shutter turns a flickering light into exactly this; in the dark it is the sensor's row
    noise."""
    d = (np.asarray(a, np.float64) - np.asarray(b, np.float64)) / math.sqrt(2)
    d = d - d.mean()
    return float(max(float(d.mean(axis=1).var()) - float(d.var()) / d.shape[1], 0.0))


def pair_temporal(a: Array, b: Array) -> tuple[float, float]:
    """(mean DN, temporal variance DN²) of two frames of the same scene."""
    a64, b64 = np.asarray(a, np.float64), np.asarray(b, np.float64)
    return float((a64.mean() + b64.mean()) / 2), float(np.var(a64 - b64) / 2)


# ----------------------------------------------------------------- darks
class DarkPoint(_Frozen):
    gain_db: float
    exposure_us: float
    black_dn: float
    read_noise_dn: float
    dsnu_dn: float


class DarkResult(_Frozen):
    points: tuple[DarkPoint, ...]
    black_dn: float  # at the lowest gain, shortest exposure
    read_noise_dn: float  # same point
    dark_current_dn_s: float  # at the lowest gain (slope over exposure)
    hot_pixels: tuple[tuple[int, int], ...]  # (row, col) in the measured plane
    hot_pixel_count: int


def dark_point(frames: Sequence[Array], gain_db: float, exposure_us: float) -> DarkPoint:
    """Stats of ≥ 2 dark frames at one setting."""
    stack = np.stack([np.asarray(f, np.float64) for f in frames])
    mean_img = stack.mean(axis=0)
    _, tvar = pair_temporal(stack[0], stack[1])
    n = len(stack)
    dsnu_var = max(float(np.var(mean_img)) - tvar / n, 0.0)
    return DarkPoint(
        gain_db=gain_db,
        exposure_us=exposure_us,
        black_dn=float(mean_img.mean()),
        read_noise_dn=math.sqrt(tvar),
        dsnu_dn=math.sqrt(dsnu_var),
    )


def hot_pixels(mean_dark: Array) -> tuple[tuple[tuple[int, int], ...], int]:
    """Robust outliers (median + HOT_SIGMA × 1.4826 × MAD) of an averaged dark: the
    ``MAX_HOT_PIXELS`` hottest (row, col) and the true count (JAI 2026-10-24: 3504, the list
    cap of 1000 had been reported as the count)."""
    med = float(np.median(mean_dark))
    mad = float(np.median(np.abs(mean_dark - med))) * 1.4826
    thresh = med + HOT_SIGMA * max(mad, 0.5)
    rows, cols = np.nonzero(mean_dark > thresh)
    order = np.argsort(-mean_dark[rows, cols])[:MAX_HOT_PIXELS]
    return tuple((int(rows[i]), int(cols[i])) for i in order), int(rows.size)


def dark_result(
    points: Sequence[DarkPoint], long_mean_dark: Array, hot_total: int | None = None
) -> DarkResult:
    base_gain = min(p.gain_db for p in points)
    base = sorted((p for p in points if p.gain_db == base_gain), key=lambda p: p.exposure_us)
    if len(base) >= 2:
        x = np.array([p.exposure_us / 1e6 for p in base])
        y = np.array([p.black_dn for p in base])
        slope = float(np.polyfit(x, y, 1)[0])
    else:
        slope = 0.0
    hot, total = hot_pixels(long_mean_dark)
    return DarkResult(
        points=tuple(points),
        black_dn=base[0].black_dn,
        read_noise_dn=base[0].read_noise_dn,
        dark_current_dn_s=max(slope, 0.0),
        hot_pixels=hot,
        hot_pixel_count=hot_total if hot_total is not None else total,
    )


# --------------------------------------------------------- photon transfer
class PtcPoint(_Frozen):
    exposure_us: float
    mean_dn: float
    var_dn2: float
    row_var_dn2: float = 0.0  # row-correlated part (flicker / row noise), see row_variance


def flicker_inflation(points: Sequence[PtcPoint], row_noise_dn2: float) -> tuple[float, float]:
    """(worst share of the temporal variance that row banding adds beyond the sensor's own
    row noise, at which exposure µs)."""
    worst, at = 0.0, 0.0
    for p in points:
        if p.var_dn2 > 0:
            share = max(p.row_var_dn2 - row_noise_dn2, 0.0) / p.var_dn2
            if share > worst:
                worst, at = share, p.exposure_us
    return worst, at


class PtcResult(_Frozen):
    points: tuple[PtcPoint, ...]
    black_dn: float
    saturation_dn: float
    system_gain_dn_per_e: float
    read_noise_e: float
    full_well_e: float
    fit_r2: float
    linearity_error_pct: float  # max deviation from a straight line, 5–90 % of range
    fit_points: int


def photon_transfer(
    points: Sequence[PtcPoint], black_dn: float, dark_read_noise_dn: float | None = None
) -> PtcResult:
    """Fit the photon transfer curve of a flat-field exposure sweep. The read noise in e- comes
    from the darks when given (EMVA 1288): the fit's intercept is too small to trust and
    came out negative on the JAI (2026-10-24: reported 0.0 e-)."""
    pts = sorted(points, key=lambda p: p.exposure_us)
    saturation = max(p.mean_dn for p in pts)
    span = max(saturation - black_dn, 1e-9)
    fit = [p for p in pts if PTC_LO * span <= p.mean_dn - black_dn <= PTC_HI * span]
    if len(fit) < 3:
        raise ValueError(f"photon transfer needs ≥ 3 points in the linear range, got {len(fit)}")
    x = np.array([p.mean_dn - black_dn for p in fit])
    y = np.array([p.var_dn2 for p in fit])
    k, c = np.polyfit(x, y, 1)
    resid = y - (k * x + c)
    tot = float(((y - y.mean()) ** 2).sum())
    r2 = 1.0 - float((resid**2).sum()) / tot if tot > 0 else 0.0
    lin_err = linearity_error(pts, black_dn)
    k = float(k)
    return PtcResult(
        points=tuple(pts),
        black_dn=black_dn,
        saturation_dn=saturation,
        system_gain_dn_per_e=k,
        read_noise_e=(
            (
                dark_read_noise_dn
                if dark_read_noise_dn is not None
                else math.sqrt(max(float(c), 0.0))
            )
            / k
            if k > 0
            else float("nan")
        ),
        full_well_e=span / k if k > 0 else float("nan"),
        fit_r2=r2,
        linearity_error_pct=lin_err,
        fit_points=len(fit),
    )


class ResponseResult(_Frozen):
    """Response of an output that photon transfer does not describe (e.g. a combined HDR
    output, where the sensor merges several channels): linearity, saturation and the
    measured SNR across the range."""

    points: tuple[PtcPoint, ...]
    black_dn: float
    saturation_dn: float
    linearity_error_pct: float  # max deviation from a straight line, 5–90 % of range
    snr: tuple[tuple[float, float], ...]  # (signal DN above black, SNR) per unclipped point


def snr_table(
    points: Sequence[PtcPoint], black_dn: float, saturation_dn: float
) -> tuple[tuple[float, float], ...]:
    """(signal, SNR) of each sweep point below 97 % of saturation."""
    out = []
    for p in sorted(points, key=lambda p: p.exposure_us):
        sig = p.mean_dn - black_dn
        if p.var_dn2 > 0 and sig > 0 and p.mean_dn < 0.97 * saturation_dn:
            out.append((sig, sig / math.sqrt(p.var_dn2)))
    return tuple(out)


def linearity_error(points: Sequence[PtcPoint], black_dn: float) -> float:
    """Max deviation (% of range) from a straight line of signal over exposure, 5–90 %."""
    pts = sorted(points, key=lambda p: p.exposure_us)
    span = max(max(p.mean_dn for p in pts) - black_dn, 1e-9)
    lin = [p for p in pts if 0.05 * span <= p.mean_dn - black_dn <= 0.90 * span]
    if len(lin) < 3:
        return 0.0
    ex = np.array([p.exposure_us for p in lin])
    sig = np.array([p.mean_dn - black_dn for p in lin])
    a, b = np.polyfit(ex, sig, 1)
    return float(np.max(np.abs(sig - (a * ex + b))) / span * 100)


def response(points: Sequence[PtcPoint], black_dn: float) -> ResponseResult:
    pts = tuple(sorted(points, key=lambda p: p.exposure_us))
    sat = max(p.mean_dn for p in pts)
    return ResponseResult(
        points=pts,
        black_dn=black_dn,
        saturation_dn=sat,
        linearity_error_pct=linearity_error(pts, black_dn),
        snr=snr_table(pts, black_dn, sat),
    )


# ------------------------------------------------------------------ flat
def clipped_var(x: Array, k: float = CLIP_SIGMA) -> float:
    """Variance after one robust k-σ clip (hot pixels and dust would dominate)."""
    v = np.asarray(x, np.float64).ravel()
    med = float(np.median(v))
    sigma = float(np.median(np.abs(v - med))) * 1.4826
    if sigma <= 0:
        return float(np.var(v))
    return float(np.var(v[np.abs(v - med) <= k * sigma]))


# ------------------------------------------------------------------ flat
class FlatResult(_Frozen):
    mean_dn: float
    vignetting_corner_ratio: float  # mean of the 4 corners / centre (1 = none)
    prnu_pct: float
    uniformity_ok: bool


def smooth_shading(img: Array) -> npt.NDArray[np.float64]:
    h, w = img.shape[:2]
    sigma = max(h, w) * SHADING_BLUR_FRAC
    return np.asarray(cv2.GaussianBlur(np.asarray(img, np.float64), (0, 0), sigma), np.float64)


def flat_result(
    mean_flat: Array, black_dn: float, temporal_var: float, n_frames: int
) -> FlatResult:
    """Vignetting and PRNU of an averaged flat field (black subtracted here)."""
    img = np.asarray(mean_flat, np.float64) - black_dn
    shade = smooth_shading(img)
    h, w = img.shape
    bh, bw = max(1, int(h * CORNER_FRAC)), max(1, int(w * CORNER_FRAC))
    corners = [shade[:bh, :bw], shade[:bh, -bw:], shade[-bh:, :bw], shade[-bh:, -bw:]]
    cy, cx = h // 2, w // 2
    centre_box = shade[cy - bh : cy + bh, cx - bw : cx + bw]
    ratio = float(np.mean([c.mean() for c in corners]) / max(centre_box.mean(), 1e-9))
    inner = centre(img / np.maximum(shade, 1e-9), 0.8)
    mean = float(img.mean())
    prnu_var = clipped_var(inner) - temporal_var / n_frames / max(mean, 1e-9) ** 2
    return FlatResult(
        mean_dn=mean + black_dn,
        vignetting_corner_ratio=ratio,
        prnu_pct=math.sqrt(max(prnu_var, 0.0)) * 100,
        uniformity_ok=ratio > 0.3,
    )


# ------------------------------------------------------------- colour
class ColourPlane(_Frozen):
    name: str
    signal_dn: float  # mean above black, centre region
    relative_to_green: float
    temporal_sigma_dn: float
    snr: float
    prnu_pct: float


class ColourResult(_Frozen):
    planes: tuple[ColourPlane, ...]
    green_imbalance_pct: float | None  # Bayer: (Gr − Gb) / mean green


def colour_result(frames: Sequence[Array], pixel_format: str, black_dn: float) -> ColourResult:
    """Response, temporal noise and PRNU of each colour plane of ≥ 2 flat-field frames."""
    per = [colour_planes(f, pixel_format) for f in frames]
    if not per or not per[0]:
        raise ValueError("no colour planes: mono data")
    n = len(per)
    stats: dict[str, tuple[float, float, float]] = {}
    for name in per[0]:
        stack = [p[name] for p in per]
        mean = np.mean(stack, axis=0)
        _, tvar = pair_temporal(centre(stack[0]), centre(stack[1]))
        signal = float(centre(mean).mean()) - black_dn
        prnu = flat_result(mean, black_dn, tvar, n).prnu_pct
        stats[name] = (signal, tvar, prnu)
    greens = [v[0] for k, v in stats.items() if k.startswith("G")]
    green = float(np.mean(greens)) if greens else float("nan")
    planes = tuple(
        ColourPlane(
            name=k,
            signal_dn=sig,
            relative_to_green=sig / green if green > 0 else float("nan"),
            temporal_sigma_dn=math.sqrt(tvar),
            snr=sig / math.sqrt(tvar) if tvar > 0 else float("nan"),
            prnu_pct=prnu,
        )
        for k, (sig, tvar, prnu) in stats.items()
    )
    imbalance = None
    if "Gr" in stats and "Gb" in stats and green > 0:
        imbalance = (stats["Gr"][0] - stats["Gb"][0]) / green * 100
    return ColourResult(planes=planes, green_imbalance_pct=imbalance)


# ---------------------------------------------------- SNR on a real scene
class LevelSnr(_Frozen):
    level: float  # mean signal above black, fraction of the range
    snr: float
    pixels: int


def snr_by_level(
    a: Array,
    b: Array,
    black_dn: float,
    span_dn: float,
    *,
    bins: int = 14,
    min_pixels: int = 400,
) -> list[LevelSnr]:
    """Temporal SNR per brightness band of a real (non-uniform) scene from two frames.

    Pixels are grouped by their mean level (log-spaced bands from 0.1 % to 97 % of the
    range); each band's noise is a robust σ (1.4826 × MAD) of (a − b)/√2, so edges that move
    a little between frames do not count as noise. Clipped pixels are left out.
    """
    a64, b64 = np.asarray(a, np.float64), np.asarray(b, np.float64)
    sig = (a64 + b64) / 2 - black_dn
    diff = (a64 - b64) / math.sqrt(2)
    lvl = sig / max(span_dn, 1e-9)
    edges = np.geomspace(0.001, 0.97, bins + 1)
    out = []
    for lo, hi in itertools.pairwise(edges):
        sel = (lvl >= lo) & (lvl < hi)
        n = int(sel.sum())
        if n < min_pixels:
            continue
        d = diff[sel]
        sigma = 1.4826 * float(np.median(np.abs(d - np.median(d))))
        s = float(sig[sel].mean())
        if sigma > 0:
            out.append(LevelSnr(level=s / span_dn, snr=s / sigma, pixels=n))
    return out


# ------------------------------------------------------------- checks
def looks_dark(frame_plane: Array, full_scale: float) -> bool:
    """Lens cap on? A low, featureless frame: median under 20 % of full scale (the black
    pedestal can be several %) and the 99th percentile within 3 % of the median."""
    med = float(np.median(frame_plane))
    p99 = float(np.percentile(frame_plane, 99))
    return med < 0.20 * full_scale and p99 - med < 0.03 * full_scale


def looks_flat(a: Array, b: Array, full_scale: float) -> tuple[bool, str]:
    """An even, steady diffuser in front of the lens? (reason when not)."""
    m_a, m_b = float(np.mean(a)), float(np.mean(b))
    if m_a < 0.05 * full_scale:
        return False, "too dark for a flat field: more light on the diffuser"
    if abs(m_a - m_b) / max(m_a, 1e-9) > 0.02:
        return False, "the flat field is moving or the light is changing: hold the diffuser still"
    img = np.asarray(a, np.float64)
    shade = smooth_shading(img)
    detail = float(np.std(centre(img / np.maximum(shade, 1e-9), 0.8)))
    if detail > 0.08:
        return (
            False,
            "the view is not even (edges or texture visible): cover the lens with the diffuser",
        )
    return True, ""
