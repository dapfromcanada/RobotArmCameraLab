"""Site light assessment: is the light on this scene good enough for this camera, and what
should change? No test jig — only the camera, whatever it looks at, and the camera model's
bench baseline (``config/camera_models/<model>/camera.yaml``), which turns the measurements into
verdicts.

Checks (each pass / warn / fail / info, with the measured value, what the camera model
should give, and the action):

1. ``camera_state`` — the design mode is on and the output is linear (exposure E and 2E:
   the signal must double; ≈ × 1.41 means a gamma LUT is on).
2. ``flicker`` — row bands (rolling shutter) and frame-to-frame changes over 16 frames; the
   band period × the row time gives the light's ripple frequency.
3. ``brightness`` — the exposure that puts the scene at the camera's best-SNR level, within
   the frame-time budget: enough light, or how much more / less.
Checks 3–5 and ``structure`` judge the camera's part area when one is saved (Phase 13);
the linearity, flicker and distribution checks use the whole image.

4. ``snr`` — temporal noise at that exposure vs what this camera model gives at that level.
5. ``range`` — clipped and black pixels, scene contrast vs the camera's dynamic range.
6. ``distribution`` — brightness on a 3 × 3 grid (scene and light cannot be separated:
   information only).

The camera is backed up first and restored afterwards unless ``apply`` is set, which leaves
it in the design mode at the recommended exposure.
"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import cv2
import numpy as np
import numpy.typing as npt
from pydantic import BaseModel, ConfigDict, Field

from camera_lab.core.paths import data_root, safe_join, validate_serial
from camera_lab.two_d.camera_models import ModelSheet, load_model_sheet, paper_paths
from camera_lab.two_d.errors import ConfigurationError
from camera_lab.two_d.fingerprint import measure as fm
from camera_lab.two_d.models import CameraSettingsPatch, is_bayer
from camera_lab.two_d.part_area import crop_plane
from camera_lab.two_d.scoring import FractionalROI, score_frame

if TYPE_CHECKING:
    from camera_lab.two_d.service import Actor, TwoDService

Status = Literal["pass", "warn", "fail", "info"]

FLICKER_FRAMES = 16
LINEAR_LO, LINEAR_HI = 0.05, 0.40  # linearity check level window (fraction of full scale)
FLICKER_WARN, FLICKER_FAIL = 0.05, 0.25  # share of the noise added by row banding
SHORT_EXPOSURE_US = 1000.0  # flicker detection: ≈ 1/8 of a mains flicker period
BANDING_VISIBLE = 3.0  # row banding vs what pixel noise alone puts in a row mean
FRAME_FLICKER_WARN_PCT = 1.0  # frame-to-frame brightness change (std, % of the level)
FRAME_FLICKER_FAIL_PCT = 5.0
SNR_WARN = 0.6  # measured / expected SNR below this → noisier than the model should be
CLIP_WARN_PCT, CLIP_FAIL_PCT = 0.5, 5.0
HIGHLIGHT_TARGET = 0.80  # expose to the right: the scene's highlights (p99) here
LIGHT_DRIFT_WARN = 0.05  # light level change between the start and the end of the check
STRUCTURE_STOPS = 1.0  # scene contrast below this ...
STRUCTURE_NOISE_FACTOR = 2.0  # ... and fine detail below this × the noise → featureless
GAIN_TOLERANCE_DB = 0.1
MAINS_HZ = (100.0, 120.0)
MAINS_TOLERANCE = 0.12


class SiteCheck(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    status: Status
    message: str
    action: str = ""
    data: dict[str, Any] = Field(default_factory=dict)


class SiteLightReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    camera_id: str
    model: str
    created_utc: str
    baseline: str  # the model sheet used, or "none"
    verdict: Literal["pass", "warn", "fail"]
    recommended_exposure_us: float | None
    checks: tuple[SiteCheck, ...]
    folder: str | None = None

    def summary(self) -> str:
        bad = [c for c in self.checks if c.status in ("warn", "fail")]
        head = f"{self.verdict.upper()}: {self.model} {self.camera_id}"
        if not bad:
            return f"{head} — the light suits this camera"
        return head + " — " + "; ".join(f"{c.name}: {c.action or c.message}" for c in bad)


def assessments_root() -> Path:
    """``<data_root>/two_d/site_light``."""
    return data_root() / "two_d" / "site_light"


class _Assessment:
    def __init__(
        self,
        service: TwoDService,
        camera_id: str,
        sheet: ModelSheet | None,
        actor: Actor,
        max_exposure_us: float | None,
    ) -> None:
        self.svc = service
        self.cam = camera_id
        self.sheet = sheet
        self.actor = actor
        self.max_exposure_us = max_exposure_us
        self.checks: list[SiteCheck] = []
        self.fmt = ""
        self.full = 0.0
        self.black = 0.0
        self.budget = 0.0  # longest exposure allowed (µs)
        self.rate0: float | None = None  # mean level per µs at the start (light drift)
        self.area: FractionalROI | None = None  # the part area in the image (None = whole)

    def part_area(self) -> None:
        """Judge the part area when the camera has one (brightness, SNR, range)."""
        params = self.svc.scoring_params(self.cam)
        if params.roi_source == "part_area":
            self.area = params.roi
        if params.roi_note or self.area is not None:
            self.add(
                SiteCheck(
                    name="area",
                    status="warn" if params.roi_note else "info",
                    message=params.roi_note
                    or "brightness, SNR and range are judged on the camera's part area",
                    action="redraw the part area on the live view" if params.roi_note else "",
                    data={"roi": self.area.model_dump() if self.area else None},
                )
            )

    # ---------------------------------------------------------- helpers
    def plane(self) -> npt.NDArray[np.float64]:
        f = self.svc.grab(self.cam, 7000)
        self.fmt = f.pixel_format
        self.full = float((1 << f.bit_depth) - 1)
        return fm.raw_plane(f.data, f.pixel_format)

    def set_exposure(self, exp: float) -> float:
        self.svc.apply(self.cam, CameraSettingsPatch(exposure_us=exp), self.actor, mode="clamp")
        return float(self.svc.get_settings(self.cam).settings.exposure_us)

    def exposure_range(self) -> tuple[float, float]:
        rng = self.svc.get_settings(self.cam).ranges["exposure_us"]
        return float(rng.min.value), float(rng.max.value)  # type: ignore[union-attr]

    def max_fps(self, shortest_us: float) -> float | None:
        """The camera's fastest frame rate in this format (read at the shortest exposure: long
        exposures lower it), not the rate it happens to be set to."""
        self.set_exposure(shortest_us)
        view = self.svc.get_settings(self.cam)
        rng = view.ranges.get("frame_rate_hz")
        top = getattr(getattr(rng, "max", None), "value", None)
        if isinstance(top, int | float) and top > 0:
            return float(top)
        return view.settings.frame_rate_hz

    def set_budget(self) -> float:
        """One frame at the format's free-run frame rate (the model sheet's measured value,
        else the camera's maximum), or the caller's maximum."""
        lo, hi = self.exposure_range()
        view = self.svc.get_settings(self.cam).settings
        start = float(view.exposure_us)
        sheet_fps = self.sheet.timing.fps_full_frame.get(view.pixel_format) if self.sheet else None
        roi_full = view.roi.x == 0 and view.roi.y == 0
        if sheet_fps and roi_full:
            fps: float | None = sheet_fps
        else:
            fps = self.max_fps(lo)
            self.set_exposure(start)
        self.budget = min(self.max_exposure_us or (1e6 / fps if fps else hi), hi)
        return self.budget

    def gain_condition(self) -> None:
        """The baseline's numbers hold at its gain; in a design mode where gain is digital any
        other value scales every level and clips the highlights."""
        want = self.sheet.performance.conditions.gain_db if self.sheet else None
        if want is None:
            return
        now = float(self.svc.get_settings(self.cam).settings.gain_db)
        if abs(now - want) <= GAIN_TOLERANCE_DB:
            return
        self.add(
            SiteCheck(
                name="gain",
                status="warn",
                message=f"gain is {now:.1f} dB; the baseline was measured at {want:.1f} dB, so "
                f"output levels are × {10 ** ((now - want) / 20):.2f} against it",
                action=f"set gain_db {want:g} (in this camera's design mode gain is digital: it "
                "adds no SNR and clips highlights)",
                data={"gain_db": now, "baseline_gain_db": want},
            )
        )

    def scored_levels(self, frames: int = 2) -> tuple[float, float]:
        """(p99, mean) as fractions of full scale, exactly as `capture` scores them."""
        params = self.svc.scoring_params(self.cam)
        hi, mean = [], []
        for _ in range(frames):
            rep = score_frame(self.svc.grab(self.cam, 7000), params)
            hi.append(rep.metric("p99"))
            mean.append(rep.metric("mean_level"))
        black = self.black / max(self.full, 1.0)
        return float(np.mean(hi)) - black, float(np.mean(mean)) - black

    def level(self, img: npt.NDArray[np.float64]) -> float:
        """Mean above black as a fraction of the range."""
        return (float(img.mean()) - self.black) / max(self.full - self.black, 1.0)

    def add(self, check: SiteCheck) -> SiteCheck:
        self.checks.append(check)
        return check

    # ------------------------------------------------------------ checks
    def camera_state(self) -> float | None:
        """Linearity at an exposure that puts the scene at 5–40 %; returns that exposure."""
        lo, hi = self.exposure_range()
        exp = float(self.svc.get_settings(self.cam).settings.exposure_us)
        img = self.plane()
        if self.sheet is not None and self.sheet.performance.black_dn is not None:
            self.black = self.sheet.performance.black_dn
        for _ in range(12):
            lvl = self.level(img)
            if LINEAR_LO <= lvl <= LINEAR_HI:
                break
            factor = 0.15 / max(lvl, 1e-4) if lvl < 0.98 else 0.25
            top = min(hi, self.budget or hi)  # E/2 and E are both within the budget
            exp = self.set_exposure(min(top, max(lo, exp * min(max(factor, 0.1), 50))))
            img = self.plane()
        lvl = self.level(img)
        if not LINEAR_LO <= lvl <= LINEAR_HI:
            self.add(
                SiteCheck(
                    name="camera_state",
                    status="warn",
                    message=f"could not bring the scene to 5–40 % (at {lvl * 100:.1f} %): "
                    "linearity not checked",
                    action="check the scene is lit and not saturated",
                    data={"level": lvl, "exposure_us": exp},
                )
            )
            return exp if 0.01 < lvl < 0.98 else None
        b = np.mean([self.plane() for _ in range(2)], axis=0)  # at E
        half = self.set_exposure(exp / 2)
        a = np.mean([self.plane() for _ in range(2)], axis=0)  # at E/2
        self.set_exposure(exp)
        mask = b < 0.9 * self.full
        ratio = (float(b[mask].mean()) - self.black) / max(float(a[mask].mean()) - self.black, 1e-9)
        ratio *= half / exp * 2  # the camera may round the halved exposure
        self.rate0 = lvl / exp
        expect = (
            self.sheet.state_checks.linear_output.expect_ratio
            if self.sheet is not None
            else (1.9, 2.1)
        )
        lut = self.sheet.state_checks.linear_output.lut_on_ratio if self.sheet else None
        data = {"ratio": ratio, "expected": list(expect), "exposure_us": exp, "level": lvl}
        if expect[0] <= ratio <= expect[1]:
            self.add(
                SiteCheck(
                    name="camera_state",
                    status="pass",
                    message=f"linear output: doubling the exposure gives × {ratio:.2f}",
                    data=data,
                )
            )
        else:
            near_lut = lut is not None and abs(ratio - lut) < 0.25
            self.add(
                SiteCheck(
                    name="camera_state",
                    status="fail",
                    message=f"doubling the exposure gives × {ratio:.2f}, not × 2"
                    + (" — a gamma LUT is on" if near_lut else ""),
                    action="set the camera's design mode"
                    + (
                        f" ({self.sheet.design_mode})"
                        if self.sheet and self.sheet.design_mode
                        else ""
                    )
                    + "; measurements on non-linear output are wrong",
                    data=data,
                )
            )
        return exp

    def _banding(self, exp: float) -> dict[str, Any]:
        """Flicker statistics of FLICKER_FRAMES frames at ``exp``."""
        self.set_exposure(exp)
        frames = [self.plane() for _ in range(FLICKER_FRAMES)]
        pairs = list(zip(frames[0::2], frames[1::2], strict=True))
        tvar = float(np.mean([fm.pair_temporal(a, b)[1] for a, b in pairs]))
        row_var = float(np.mean([fm.row_variance(a, b) for a, b in pairs]))
        sheet_rows = self.sheet.performance.row_noise_dn if self.sheet else None
        own = (sheet_rows or 0.0) ** 2
        share = max(row_var - own, 0.0) / max(tvar, 1e-9)
        width = frames[0].shape[1]
        # banding relative to what pixel noise alone puts in a row mean: detects flicker even
        # where it adds little to the noise (dim scene, short exposure)
        visible = math.sqrt(max(row_var - own, 0.0)) / max(math.sqrt(tvar / width), 1e-9)
        means = np.array([f.mean() for f in frames])
        frame_pct = float(means.std()) / max(float(means.mean()) - self.black, 1e-9) * 100
        stack = np.stack(frames)
        rows = (stack - stack.mean(axis=0)).mean(axis=2)
        spec = np.abs(np.fft.rfft(rows, axis=1)).mean(axis=0)
        spec[0] = 0
        k = int(np.argmax(spec))
        if 1 <= k < len(spec) - 1:  # parabolic interpolation between FFT bins
            y0, y1, y2 = spec[k - 1], spec[k], spec[k + 1]
            den = y0 - 2 * y1 + y2
            kf = k + (0.5 * (y0 - y2) / den if den != 0 else 0.0)
        else:
            kf = float(k)
        period_rows = rows.shape[1] / kf if kf > 0 else float("inf")
        return {
            "exposure_us": exp,
            "band_period_sensor_rows": period_rows * (2 if is_bayer(self.fmt) else 1),
            "row_banding_share": share,
            "banding_vs_row_noise": visible,
            "frame_change_pct": frame_pct,
            "band_period_rows": period_rows,
        }

    def _ripple_hz(self, period_rows: float) -> float | None:
        rolling = self.sheet is not None and self.sheet.sensor.shutter == "rolling"
        row_time = self.sheet.timing.row_time_us if self.sheet else None
        sensor_rows = period_rows * (2 if is_bayer(self.fmt) else 1)
        if rolling and row_time and math.isfinite(sensor_rows):
            return 1e6 / (sensor_rows * row_time)
        return None

    def detect_flicker(self) -> dict[str, Any]:
        """Flicker at a short exposure: is it there, at what frequency, is it mains?"""
        lo, _ = self.exposure_range()
        short = self._banding(max(lo, SHORT_EXPOSURE_US))
        present = short["banding_vs_row_noise"] > BANDING_VISIBLE
        freq = self._ripple_hz(short["band_period_rows"]) if present else None
        mains = next((m for m in MAINS_HZ if freq and abs(freq - m) / m < MAINS_TOLERANCE), None)
        return {"short": short, "present": present, "ripple_hz": freq, "mains_hz": mains}

    def flicker(self, exp: float, det: dict[str, Any]) -> None:
        """What remains of the flicker at the recommended exposure ``exp``."""
        short, present, freq, mains = (
            det["short"],
            det["present"],
            det["ripple_hz"],
            det["mains_hz"],
        )
        work = self._banding(exp)
        share, frame_pct = work["row_banding_share"], work["frame_change_pct"]
        data: dict[str, Any] = {"short": short, "working": work, "ripple_hz": freq}
        if mains is not None:
            what = f"mains flicker ≈ {freq:.0f} Hz ({mains / 2:.0f} Hz mains)"
            safe = (
                f"exposures in whole multiples of {1e3 / mains:.2f} ms (or ≥ "
                f"{10e3 / mains:.0f} ms) average it out; or use DC (flicker-free) lighting"
            )
        elif present and freq is not None:
            what = f"light ripple ≈ {freq:.0f} Hz"
            safe = f"exposures ≥ {10e3 / freq:.1f} ms (10 ripple periods) average it out"
        elif present:
            what = "the light ripples"
            safe = "use longer exposures or a steadier light source"
        else:
            what, safe = "", ""
        bad_now = share > FLICKER_WARN or frame_pct > FRAME_FLICKER_WARN_PCT
        if not present and not bad_now:
            self.add(
                SiteCheck(
                    name="flicker",
                    status="pass",
                    message=f"steady light (no bands at {short['exposure_us'] / 1000:.2f} ms; at "
                    f"the recommended {exp / 1000:.2f} ms row banding adds {share * 100:.1f} % to "
                    f"the noise, brightness varies {frame_pct:.2f} % between frames)",
                    data=data,
                )
            )
            return
        if not bad_now:
            self.add(
                SiteCheck(
                    name="flicker",
                    status="warn",
                    message=f"{what}: bands at {short['exposure_us'] / 1000:.2f} ms; at the "
                    f"recommended {exp / 1000:.2f} ms it averages out ({share * 100:.1f} % added "
                    "noise)",
                    action=f"keep it that way: {safe}",
                    data=data,
                )
            )
            return
        status: Status = (
            "fail" if share > FLICKER_FAIL or frame_pct > FRAME_FLICKER_FAIL_PCT else "warn"
        )
        if not what:
            what = f"the brightness changes {frame_pct:.1f} % between frames"
            safe = "use a steadier light source (or average several frames)"
        self.add(
            SiteCheck(
                name="flicker",
                status=status,
                message=f"{what}: at the recommended {exp / 1000:.2f} ms row banding adds "
                f"{share * 100:.0f} % to the noise; brightness varies {frame_pct:.2f} % between "
                "frames",
                action=safe,
                data=data,
            )
        )

    def brightness(self, exp: float, mains: float | None) -> float:
        """Exposure that puts the scene's highlights (p99) at HIGHLIGHT_TARGET of full scale
        ("expose to the right": a linear sensor's SNR is best near the top), within the
        budget; whole flicker periods when the light flickers at mains frequency. The levels
        come from the same scoring as `capture` (debayered luminance), so both agree."""
        lo, hi = self.exposure_range()
        exp = self.set_exposure(exp)
        for _ in range(8):  # back off until the highlights are not clipped
            hi_lvl, lvl = self.scored_levels()
            if hi_lvl < 0.95 or exp <= lo:
                break
            exp = self.set_exposure(max(lo, exp / 4))
        target = HIGHLIGHT_TARGET
        rate = max(hi_lvl, 1e-9) / exp  # highlight level per µs (linear output)
        mean_rate = lvl / exp
        budget = self.budget or hi
        want = target / rate
        rec = min(max(want, lo), budget)
        if mains is not None:  # whole flicker periods cancel the flicker
            half = 1e6 / mains
            if rec >= half:
                rec = max(half, math.floor(rec / half) * half)
        data = {
            "highlight_target": target,
            "highlight_percentile": 99,  # as the capture scores (level_metric p99)
            "exposure_needed_us": want,
            "exposure_budget_us": budget,
            "recommended_exposure_us": rec,
            "highlight_level_at_recommended": rate * rec,
            "mean_level_at_recommended": mean_rate * rec,
        }
        if want > budget:
            more = want / budget
            self.add(
                SiteCheck(
                    name="brightness",
                    status="fail" if more > 2 else "warn",
                    message=f"too dark: highlights at {target * 100:.0f} % of full scale (best "
                    f"SNR) need {want / 1000:.1f} ms; the budget is {budget / 1000:.1f} ms "
                    f"(highlights reach {rate * budget * 100:.1f} %)",
                    action=f"about {more:.1f}× more light (or a wider iris); never more gain",
                    data=data,
                )
            )
        elif want < lo:
            less = lo / want
            self.add(
                SiteCheck(
                    name="brightness",
                    status="fail" if less > 2 else "warn",
                    message=f"too bright: even the shortest exposure ({lo:.0f} µs) puts the "
                    f"highlights above {target * 100:.0f} % of full scale",
                    action=f"about {less:.1f}× less light (or a smaller iris)",
                    data=data,
                )
            )
        else:
            self.add(
                SiteCheck(
                    name="brightness",
                    status="pass",
                    message=f"enough light: {rec / 1000:.2f} ms puts the highlights at "
                    f"{rate * rec * 100:.0f} % and the mean at {mean_rate * rec * 100:.1f} % of "
                    f"full scale (budget {budget / 1000:.1f} ms)",
                    data=data,
                )
            )
        return rec

    def snr_and_range(self, exp: float) -> None:
        exp = self.set_exposure(exp)
        whole = self.plane()
        a, b = crop_plane(whole, self.area), crop_plane(self.plane(), self.area)
        lvl = self.level(whole)  # the light drift compares whole images
        if self.rate0:
            drift = (lvl / exp) / self.rate0 - 1
            self.add(
                SiteCheck(
                    name="light_stability",
                    status="warn" if abs(drift) > LIGHT_DRIFT_WARN else "pass",
                    message=f"the light changed {drift * 100:+.1f} % during the check",
                    action="the light is not steady (daylight, a light warming up, a person "
                    "moving): repeat the assessment when it is, or shield the scene"
                    if abs(drift) > LIGHT_DRIFT_WARN
                    else "",
                    data={"drift": drift},
                )
            )
        bands = fm.snr_by_level(a, b, self.black, self.full - self.black)
        curve = self.sheet.performance.snr_vs_signal if self.sheet else None
        rows: list[dict[str, Any]] = [
            {
                "level": band.level,
                "snr": band.snr,
                "expected": curve.at(band.level) if curve is not None else None,
                "pixels": band.pixels,
            }
            for band in bands
        ]
        ratios = [float(r["snr"]) / float(r["expected"]) for r in rows if r["expected"]]
        data = {"level": self.level(a), "exposure_us": exp, "bands": rows}
        sample = ", ".join(
            f"{r['level'] * 100:.2g} %: {r['snr']:.0f}"
            + (f" (model {r['expected']:.0f})" if r["expected"] else "")
            for r in rows[:: max(1, len(rows) // 4)]
        )
        if not rows:
            self.add(
                SiteCheck(
                    name="snr",
                    status="info",
                    message="too few pixels per brightness band to measure SNR",
                    data=data,
                )
            )
        elif not ratios:
            self.add(
                SiteCheck(
                    name="snr",
                    status="info",
                    message=f"SNR by brightness: {sample} (no baseline for this model)",
                    data=data,
                )
            )
        else:
            ratio = float(np.median(ratios))
            data["median_ratio_to_model"] = ratio
            ok = ratio >= SNR_WARN
            self.add(
                SiteCheck(
                    name="snr",
                    status="pass" if ok else "warn",
                    message=f"SNR at {len(rows)} brightness levels is {ratio:.2f}× what this "
                    f"camera model gives (sample: {sample})",
                    action=""
                    if ok
                    else "noisier than the camera should be: check that the camera and the "
                    "scene are still (vibration reads as noise), then flicker, heat or a camera "
                    "fault (cap-on dark check)",
                    data=data,
                )
            )
        # range: clipped / black pixels, scene contrast
        clipped = float(np.mean(a >= 0.98 * self.full) * 100)
        noise_floor = self.black + 3 * (
            self.sheet.performance.read_noise_dn
            if self.sheet and self.sheet.performance.read_noise_dn
            else 0.002 * self.full
        )
        black_pct = float(np.mean(a <= noise_floor) * 100)
        lo_p, hi_p = (float(np.percentile(a, q)) - self.black for q in (0.5, 99.5))
        contrast = math.log2(max(hi_p, 1.0) / max(lo_p, 1.0))
        cam_range = self.sheet.performance.dynamic_range_stops if self.sheet else None
        data = {
            "clipped_pct": clipped,
            "black_pct": black_pct,
            "scene_contrast_stops": contrast,
            "camera_range_stops": cam_range,
        }
        status: Status = "pass"
        action = ""
        if clipped > CLIP_FAIL_PCT:
            status, action = "fail", "highlights clip at the recommended exposure: less light "
            action += "on the bright parts, or a camera with more dynamic range"
        elif clipped > CLIP_WARN_PCT or black_pct > 5:
            status = "warn"
            action = "check the important parts are neither clipped nor in the noise"
        self.add(
            SiteCheck(
                name="range",
                status=status,
                message=f"{clipped:.2f} % clipped, {black_pct:.1f} % in the noise floor; scene "
                f"contrast {contrast:.1f} stops"
                + (f" (camera ≈ {cam_range:.0f})" if cam_range else ""),
                action=action,
                data=data,
            )
        )
        # structure: is there anything to see (edges, texture) beyond the noise?
        signal = max(float(a.mean()) - self.black, 1e-9)
        _, tvar = fm.pair_temporal(a, b)
        avg = (a + b) / 2
        detail = avg - cv2.GaussianBlur(avg, (0, 0), 3.0)
        texture = 1.4826 * float(np.median(np.abs(detail - np.median(detail)))) / signal
        noise = math.sqrt(tvar / 2) / signal  # what noise alone leaves in the 2-frame mean
        featureless = contrast < STRUCTURE_STOPS and texture < STRUCTURE_NOISE_FACTOR * noise
        self.add(
            SiteCheck(
                name="structure",
                status="warn" if featureless else "pass",
                message=(
                    f"no structure in view: contrast {contrast:.1f} stops, texture "
                    f"{texture / max(noise, 1e-12):.1f}× the noise"
                    if featureless
                    else f"the scene has structure (contrast {contrast:.1f} stops, texture "
                    f"{texture / max(noise, 1e-12):.1f}× the noise)"
                ),
                action="a diffuser or cover on the lens, or a blank surface? Light and SNR are "
                "fine to judge, but focus cannot be judged here: aim at the working scene"
                if featureless
                else "",
                data={"contrast_stops": contrast, "texture_vs_noise": texture / max(noise, 1e-12)},
            )
        )
        # distribution: 3 × 3 grid of the whole image
        a = whole
        h, w = a.shape
        grid = [
            [
                float(a[i * h // 3 : (i + 1) * h // 3, j * w // 3 : (j + 1) * w // 3].mean())
                - self.black
                for j in range(3)
            ]
            for i in range(3)
        ]
        c = max(grid[1][1], 1.0)
        rel = [[round(v / c, 2) for v in row] for row in grid]
        spread = min(min(r) for r in rel) / max(max(max(r) for r in rel), 1e-9)
        self.add(
            SiteCheck(
                name="distribution",
                status="info",
                message="brightness by region (centre = 1): "
                + " / ".join(" ".join(f"{v:.2f}" for v in row) for row in rel)
                + (" — strongly uneven" if spread < 0.3 else ""),
                data={"grid_relative_to_centre": rel},
            )
        )


def assess_light(
    service: TwoDService,
    camera_id: str,
    *,
    actor: Actor = "human",
    max_exposure_us: float | None = None,
    apply: bool = False,
    reset: bool = True,
    sheet_root: Path | None = None,
    out_root: Path | None = None,
    save: bool = True,
) -> SiteLightReport:
    """Assess the light on the scene the camera sees now (see the module docstring)."""
    serial = service.resolve(camera_id)
    if not service.is_open(serial):
        service.open(serial, "site-light")
    info = service._open[serial].camera.info
    sheet = load_model_sheet(info.model, sheet_root)
    backup, _ = service.backup(serial, "site light assessment")
    a = _Assessment(service, serial, sheet, actor, max_exposure_us)
    rec: float | None = None
    try:
        prof = service.get_profile(serial)
        mode = sheet.design_mode if sheet and sheet.design_mode in prof.modes else None
        if reset:  # never trust the boot state: factory Default, then the design mode
            try:
                out = service.reset_camera(serial, mode, actor)
            except ConfigurationError:  # a driver that cannot load user sets
                service.set_mode(serial, mode, actor)
            else:
                a.add(
                    SiteCheck(
                        name="start_state",
                        status="pass" if out["ok"] else "fail",
                        message=f"factory Default loaded, mode {out['mode']} applied"
                        + ("" if out["ok"] else ": " + "; ".join(out["mismatches"])),
                        action="" if out["ok"] else "the camera did not take the mode's settings",
                        data={"mismatches": out["mismatches"], "mode": out["mode"]},
                    )
                )
        else:
            service.set_mode(serial, mode, actor)
        a.set_budget()
        a.gain_condition()
        a.part_area()
        exp = a.camera_state()
        if exp is not None:
            det = a.detect_flicker()
            rec = a.brightness(exp, det["mains_hz"])
            a.flicker(rec, det)
            a.snr_and_range(rec)
    finally:
        if apply and rec is not None:
            a.set_exposure(rec)
        else:
            service.restore_backup(serial, backup.backup_id, actor)
    statuses = {c.status for c in a.checks}
    worst: Literal["pass", "warn", "fail"] = (
        "fail" if "fail" in statuses else "warn" if "warn" in statuses else "pass"
    )
    report = SiteLightReport(
        camera_id=serial,
        model=info.model,
        created_utc=datetime.now(UTC).isoformat(),
        baseline=str(paths[1])
        if sheet and (paths := paper_paths(info.model, sheet_root))
        else "none",
        verdict=worst,
        recommended_exposure_us=rec,
        checks=tuple(a.checks),
    )
    if save:
        report = _save(report, out_root)
    return report


def _save(report: SiteLightReport, out_root: Path | None) -> SiteLightReport:
    base = safe_join(out_root or assessments_root(), validate_serial(report.camera_id))
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    folder = safe_join(base, stamp)
    n = 1
    while folder.exists():
        folder = safe_join(base, f"{stamp}-{n}")
        n += 1
    folder.mkdir(parents=True)
    report = report.model_copy(update={"folder": str(folder)})
    (folder / "report.json").write_text(json.dumps(report.model_dump(mode="json"), indent=2))
    lines = [
        f"# Site light assessment — {report.model} {report.camera_id}",
        "",
        f"- **Verdict:** {report.verdict}",
        f"- **Baseline:** {report.baseline}",
        "- **Recommended exposure:** "
        + (
            f"{report.recommended_exposure_us / 1000:.2f} ms"
            if report.recommended_exposure_us
            else "-"
        ),
        f"- **When:** {report.created_utc}",
        "",
        "| check | status | result | action |",
        "|---|---|---|---|",
    ]
    lines += [f"| {c.name} | {c.status} | {c.message} | {c.action} |" for c in report.checks]
    (folder / "report.md").write_text("\n".join(lines) + "\n")
    return report
