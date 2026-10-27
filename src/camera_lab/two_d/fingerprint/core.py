"""Core measurements every camera gets (comparable like for like): identity, timing,
darks, flat field, noise vs gain, user sets."""

from __future__ import annotations

import math
import os
import platform
import statistics
import time
from typing import TYPE_CHECKING, Any

import numpy as np

from camera_lab.core.safety import NotAdjustable
from camera_lab.two_d.fingerprint import measure as fm
from camera_lab.two_d.models import PIXEL_FORMAT_BITS

if TYPE_CHECKING:
    pass
from camera_lab.two_d.errors import ConfigurationError
from camera_lab.two_d.fingerprint.base import SessionBase
from camera_lab.two_d.fingerprint.common import (
    BLACK_RAISE,
    CHANNEL_PREFIX,
    DARK_EXPOSURES_US,
    DARK_FRAMES,
    DARK_GAIN_STEP_DB,
    DARK_REF_EXPOSURE_US,
    FLAT_MASTER_FRAMES,
    FLAT_MASTER_LEVEL,
    FLAT_SWEEP,
    FLAT_TARGET,
    GAIN_SWITCH_GAIN,
    HOT_PIXEL_EXPOSURE_US,
    MAX_EXPOSURE_US,
    NOISE_PAIRS,
    NOISE_TARGET,
    PIPELINE_FRAMES,
    SETTLE_FRAMES,
    SETTLE_MAX_HALVINGS,
    SETTLE_MAX_LEVEL,
    SWEEP_LOW_FACTOR,
    FpResult,
    format_family,
    iris_hint,
)
from camera_lab.two_d.processing import demosaic, to_float01


class CoreMeasurements(SessionBase):
    """The core module."""

    def step_identity(self) -> FpResult:
        b, _ = self.service.backup(self.camera_id, "camera fingerprint: before any change")
        self.backup_id = b.backup_id
        start = self._settings()
        # a brightness the human can see by, restored after the darks (Derek 2026-10-21:
        # the camera was left too dark to position the diffuser)
        self._view_exposure, self._view_gain = float(start.exposure_us), float(start.gain_db)
        prof = self.service.get_profile(self.camera_id)
        self.mode, mode = prof.mode()  # the design mode: the profile's default
        reset = None
        try:  # never trust the boot state: factory Default, then the design mode
            if self.reset_on_start:
                reset = self.service.reset_camera(self.camera_id, self.mode, self.actor)
        except ConfigurationError:  # a driver that cannot load user sets
            reset = None
        if reset is None:
            self.service.set_mode(self.camera_id, self.mode, self.actor)
        self.gain_knob = "gain_db" in mode.knobs
        view = self.service.get_settings(self.camera_id)
        g_rng = view.ranges.get("gain_db")
        # measured on every camera with a gain control, also when the mode pins it: the
        # paper describes the camera (Derek 2026-10-24)
        self.gain_adjustable = (
            g_rng is not None
            and not isinstance(g_rng, NotAdjustable)
            and float(g_rng.max.value) > float(g_rng.min.value)
        )
        options = view.options.get("pixel_format", ())
        family = format_family(view.settings.pixel_format)
        same = [f for f in options if format_family(f) == family] or list(options)
        best12 = max(
            same, key=lambda f: (min(PIXEL_FORMAT_BITS.get(f, 8), 12), -PIXEL_FORMAT_BITS.get(f, 8))
        )
        own = mode.settings.pixel_format
        self.fmt = own if own in options and PIXEL_FORMAT_BITS.get(own, 8) >= 12 else best12
        self.channel_fmt = best12
        outputs = view.options.get("hdr.output", ())
        self.hdr_channels = tuple(o for o in outputs if o.startswith(CHANNEL_PREFIX))
        if self.gain_knob:
            self._set(pixel_format=self.fmt, gain_db=self._range("gain_db")[0])
        else:
            self._set(pixel_format=self.fmt)
        black_raised = None
        black = view.ranges.get("black.level")
        current = view.settings.advanced.get("black.level")
        # measure at the design mode's own black level where it pins one (TRI089S-M raw: 1,
        # 2026-10-27); raise it only for HDR channel outputs (12-bit, noise ≈ 45 DN at black)
        # or where the mode leaves it at the camera's (often 0: dark noise clipped)
        pinned = (mode.settings.advanced or {}).get("black.level") is not None
        if (
            black is not None
            and not isinstance(black, NotAdjustable)
            and (self.hdr_channels or not pinned)
        ):
            target = min(BLACK_RAISE, float(black.max.value))
            if isinstance(current, int | float) and current < target:
                self._set(advanced={"black.level": target})
                black_raised = target
        self.full_scale = float((1 << PIXEL_FORMAT_BITS[self.fmt]) - 1)
        self.mode_gain_db = float(self._settings().gain_db)
        cam = self.service._open[self.camera_id].camera
        info = cam.info
        nodes = cam.identity_nodes()
        s = self._settings()
        data = {
            "serial": info.serial,
            "vendor": info.vendor,
            "model": info.model,
            "firmware": nodes.get("DeviceFirmwareVersion") or info.firmware,
            "device_version": nodes.get("DeviceVersion"),
            "identity_nodes": nodes,
            "driver": self.service._open[self.camera_id].driver,
            "sensor_px": [s.roi.width, s.roi.height],
            "pixel_format": self.fmt,
            "bit_depth": PIXEL_FORMAT_BITS[self.fmt],
            "pixel_formats": list(options),
            "backup_id": b.backup_id,
            "profile": prof.label,
            "mode": self.mode,
            "gain_is_knob": self.gain_knob,
            "gain_adjustable": self.gain_adjustable,
            "temperature_c": self._temperature(),
            "hdr_channels": list(self.hdr_channels),
            "channel_pixel_format": self.channel_fmt if self.hdr_channels else None,
            "black_level_raised_to": black_raised,
            "sensor_colour": self.colour,
            "start_state": (
                {"loaded": reset["loaded"], "mismatches": reset["mismatches"]} if reset else None
            ),
            # the numbers hold at these settings (a digital gain would scale every level)
            "conditions": {
                "gain_db": float(self._settings().gain_db),
                "black_level": self._settings().advanced.get("black.level"),
                "exposure_mode": "linear" if not self.hdr_channels else "hdr_merged",
            },
            "format_family": format_family(self.fmt),
            "lens": self.lens,
        }
        bits = PIXEL_FORMAT_BITS[self.fmt]
        family = format_family(self.fmt)
        families: dict[str, tuple[str, ...]] = {
            "bayer": ("Bayer",),
            "prism": ("RGB", "BGR"),
            "mono": ("Mono",),
        }
        expect = families.get(self.colour)
        mismatch = expect is not None and not family.startswith(expect)
        note = (
            "" if bits >= 12 else f" (only {bits}-bit raw available: the noise floor may be hidden)"
        )
        return self._record(
            FpResult(
                step="identity",
                status="pass",
                message=f"{info.model} s/n {info.serial} firmware {data['firmware']}: "
                f"mode {self.mode}, measuring in "
                f"{self.fmt}{note}"
                + (f", {len(self.hdr_channels)} HDR channels" if self.hdr_channels else "")
                + (f", black level raised to {black_raised:g}" if black_raised else "")
                + (
                    f" (the profile says {self.colour}, the camera gives {family}: check the "
                    "profile's characteristics.colour)"
                    if mismatch
                    else ""
                ),
                data=data,
            )
        )

    def _pipeline_timing(self, frames: int = PIPELINE_FRAMES) -> dict[str, Any]:
        """In the design format: the interval between received frames (the camera's real
        throughput, unpacking included) and this PC's processing to a usable image
        (debayer for Bayer, conversion to float) — production throughput numbers."""
        f = self.service.grab(self.camera_id, int(MAX_EXPOSURE_US / 1000) + 5000)
        t0 = time.perf_counter()
        for _ in range(frames):
            f = self.service.grab(self.camera_id, int(MAX_EXPOSURE_US / 1000) + 5000)
        interval = (time.perf_counter() - t0) / frames
        t1 = time.perf_counter()
        for _ in range(3):
            to_float01(demosaic(f.data, f.pixel_format), f.bit_depth)
        process = (time.perf_counter() - t1) / 3
        return {
            "pixel_format": f.pixel_format,
            "exposure_us": float(self._settings().exposure_us),
            "frame_interval_ms": interval * 1000,
            "process_ms": process * 1000,
            "frame_mb": f.data.nbytes / 1e6,
            "pc": f"{platform.processor() or platform.machine()}, {os.cpu_count()} CPUs",
        }

    def step_timing(self) -> FpResult:
        self._require("identity")
        view = self.service.get_settings(self.camera_id)
        fps: dict[str, float | None] = {}
        for fmt in view.options.get("pixel_format", ()):
            self._set(pixel_format=fmt)
            fps[fmt] = self._settings().frame_rate_hz
        self._set(pixel_format=self.fmt)
        lo, hi = self._range("exposure_us")
        start_exp = exp = float(self._settings().exposure_us)
        m0 = float(np.mean(self._plane()))
        for _ in range(SETTLE_MAX_HALVINGS):  # a saturated start shows no change (JAI 2026-10-24)
            if m0 < SETTLE_MAX_LEVEL * self.full_scale or exp / 2 < lo:
                break
            exp /= 2
            self._set(exposure_us=exp)
            for _ in range(2):  # past the stale frame(s)
                self._plane()
            m0 = float(np.mean(self._plane()))
        new = min(exp * 2, hi) if exp * 2 <= hi else max(exp / 2, lo)
        self._set(exposure_us=new)
        means = [float(np.mean(self._plane())) for _ in range(SETTLE_FRAMES)]
        self._set(exposure_us=start_exp)
        change = means[-1] - m0
        stale = sum(1 for m in means if abs(change) > 0 and (m - m0) / change < 0.5)
        pipe = self._pipeline_timing()
        data = {
            "fps_full_frame": fps,
            "stale_frames_after_change": stale,
            "settle_test_level_change_dn": change,
            "pipeline": pipe,
        }
        note = (
            ""
            if abs(change) > 0.02 * self.full_scale
            else " (scene too dark or saturated to test settling)"
        )
        return self._record(
            FpResult(
                step="timing",
                status="pass",
                message=f"fps per format recorded; {stale} stale frame(s) after an exposure "
                f"change{note}; {self.fmt}: a frame every {pipe['frame_interval_ms']:.0f} ms, "
                f"{pipe['process_ms']:.0f} ms on this PC to a usable image",
                data=data,
            )
        )

    def _gain_modes(self) -> list[str | None]:
        """The design gain mode first, then the camera's other gain mode (prism cameras:
        ``prism.gain_mode``); [None] where the camera has only one."""
        now = self._settings().advanced.get("prism.gain_mode")
        if now not in ("master", "individual"):
            return [None]
        return [now, "individual" if now == "master" else "master"]

    def _gain_steps(self) -> list[float]:
        """Every gain step (DARK_GAIN_STEP_DB) of the camera's range in the current mode."""
        if not self.gain_adjustable:
            return [self._gain()]
        lo, hi = self._range("gain_db")
        n = int((hi - lo) / DARK_GAIN_STEP_DB + 1e-6)
        return [round(lo + i * DARK_GAIN_STEP_DB, 3) for i in range(n + 1)]

    def step_dark(self) -> FpResult:
        """The human has put the lens cap on. Every exposure from 1 ms to 2 s at the design
        gain, and every gain step at 10 ms in each gain mode (Derek 2026-10-25: complete)."""
        self._require("identity")
        g_design = self._gain()
        e_lo, e_hi = self._range("exposure_us")
        exposures = sorted(
            {min(max(e, e_lo), min(e_hi, MAX_EXPOSURE_US)) for e in DARK_EXPOSURES_US}
        )
        ref_exp = min(max(DARK_REF_EXPOSURE_US, e_lo), e_hi)
        hot_exp = min(exposures, key=lambda e: abs(math.log(e / HOT_PIXEL_EXPOSURE_US)))
        # the cap check at 100 ms: at 2 s the sensor's own dark signal is uneven enough to fail
        # it with no light at all (JAI 2026-10-25); a cap that leaks only at long exposures shows
        # as dark current in the results
        self._set(gain_db=g_design, exposure_us=hot_exp)
        if not fm.looks_dark(self._plane(), self.full_scale):
            return self._record(
                FpResult(
                    step="dark",
                    status="fail",
                    message="light reaches the sensor: put the lens cap on",
                )
            )
        points = []
        hot_by_exposure: list[dict[str, float]] = []
        hot_mean = None
        for exp in exposures:
            self._set(gain_db=g_design, exposure_us=exp)
            frames = [self._plane() for _ in range(DARK_FRAMES)]
            points.append(fm.dark_point(frames, g_design, exp))
            mean = np.mean(frames, axis=0)
            hot_by_exposure.append({"exposure_us": exp, "hot_pixels": fm.hot_pixels(mean)[1]})
            if exp == ref_exp:
                self._arrays["master_dark"] = mean.astype(np.float32)
                self._row_noise_dn2["own"] = fm.row_variance(frames[0], frames[1])
            if exp == hot_exp:
                hot_mean = mean
        gain_points: list[dict[str, Any]] = []
        modes = self._gain_modes()
        for mode in modes:
            if mode is not None:
                self._set(advanced={"prism.gain_mode": mode})
            for g in self._gain_steps():
                if not self._gain_writable(g):  # JAI individual mode: Gain is read-only
                    break
                self._set(exposure_us=ref_exp)
                pt = fm.dark_point([self._plane() for _ in range(DARK_FRAMES)], g, ref_exp)
                if mode == modes[0] and g != g_design:
                    points.append(pt)
                gain_points.append({**pt.model_dump(mode="json"), "gain_mode": mode})
        if modes[0] is not None:
            self._set(advanced={"prism.gain_mode": modes[0]})
        channels: dict[str, Any] = {}
        if self.hdr_channels:
            exp = ref_exp
            self._set(gain_db=g_design, exposure_us=exp)
            for out in self.hdr_channels:
                self._output(out, self.channel_fmt)
                frames = [self._plane() for _ in range(DARK_FRAMES)]
                pt = fm.dark_point(frames, g_design, exp)
                self._channel_black[out] = pt
                self._row_noise_dn2[out] = fm.row_variance(frames[0], frames[1])
                channels[out] = {
                    **pt.model_dump(mode="json"),
                    "row_noise_dn": math.sqrt(self._row_noise_dn2[out]),
                }
            self._output("HDR", self.fmt)
        self._set(exposure_us=self._view_exposure)  # viewable again
        self._set(gain_db=self._view_gain if self.gain_knob else self.mode_gain_db)
        if hot_mean is None:  # pragma: no cover - DARK_EXPOSURES_US is never empty
            raise RuntimeError("no dark frames")
        res = fm.dark_result(points, hot_mean)
        self.black_dn = res.black_dn
        data = res.model_dump(mode="json", exclude={"hot_pixels"})
        data["hot_pixels_first"] = [list(p) for p in res.hot_pixels[:50]]
        data["row_noise_dn"] = math.sqrt(self._row_noise_dn2.get("own", 0.0))
        data["gain_points"] = gain_points  # every gain step, each gain mode, at 10 ms
        data["hot_pixels_by_exposure"] = hot_by_exposure
        data["hot_pixel_exposure_us"] = hot_exp
        if channels:
            data["channels"] = channels
        ch_note = ""
        if channels:
            ch_note = (
                "; channels: "
                + ", ".join(
                    f"{k[len(CHANNEL_PREFIX) :]} {v.black_dn:.1f}/{v.read_noise_dn:.2f} DN"
                    for k, v in self._channel_black.items()
                )
                + " (black/read noise)"
            )
        longest = points[len(exposures) - 1]
        return self._record(
            FpResult(
                step="dark",
                status="pass",
                message=f"black {res.black_dn:.1f} DN, read noise {res.read_noise_dn:.2f} DN, "
                f"dark current {res.dark_current_dn_s:.1f} DN/s, {res.hot_pixel_count} hot pixels "
                f"at {hot_exp / 1000:g} ms; {len(exposures)} exposures to "
                f"{exposures[-1] / 1e6:g} s (read noise {longest.read_noise_dn:.2f} DN there), "
                f"{len(gain_points)} gain steps"
                + (f" in {len(modes)} gain modes" if modes[0] is not None else "")
                + ch_note,
                data=data,
            )
        )

    def step_flat(self) -> FpResult:
        """The human holds the opal diffuser close to the lens."""
        self._require("dark")
        e_lo, e_hi = self._lit_range()
        self._set(gain_db=self._gain())
        a, b = self._plane(), self._plane()
        ok, why = fm.looks_flat(a, b, self.full_scale)
        if not ok and "too dark" not in why:
            return self._record(FpResult(step="flat", status="fail", message=why))
        ref = self._exposure_for(FLAT_TARGET, e_lo, e_hi)
        if ref is None or ref < SWEEP_LOW_FACTOR * e_lo:
            if ref is None and self._last_fraction < FLAT_TARGET:
                why = "cannot reach 80 % of full scale: more light on the diffuser"
            else:
                factor = SWEEP_LOW_FACTOR * e_lo / ref if ref else SWEEP_LOW_FACTOR
                why = (
                    f"too bright for a full sweep from the {e_lo / 1000:g} ms minimum exposure: "
                    f"about {factor:.1f}× less light" + iris_hint(factor, self.lens)
                )
            return self._record(FpResult(step="flat", status="fail", message=why))
        a, b = self._plane(), self._plane()
        ok, why = fm.looks_flat(a, b, self.full_scale)
        if not ok:
            return self._record(FpResult(step="flat", status="fail", message=why))
        points = []
        for k in FLAT_SWEEP:
            exp = min(e_hi, max(e_lo, ref * k))
            self._set(exposure_us=exp)
            points.append(self._pair_point(exp))
        flicker, flicker_why = self._flicker_problem(points, "own")
        if flicker_why is not None:
            return self._record(
                FpResult(step="flat", status="fail", message=flicker_why, data={"flicker": flicker})
            )
        resp = fm.response(points, self.black_dn)
        ptc = None
        if not self.hdr_channels:  # merged HDR data: no photon transfer (EMVA 1288 per channel)
            try:
                dark = self.results.get("dark")
                rn = dark.data.get("read_noise_dn") if dark is not None else None
                ptc = fm.photon_transfer(points, self.black_dn, rn)
            except ValueError as exc:
                return self._record(FpResult(step="flat", status="fail", message=str(exc)))
        clipped = resp.saturation_dn >= 0.97 * self.full_scale
        self._set(exposure_us=min(e_hi, max(e_lo, ref * FLAT_MASTER_LEVEL / FLAT_TARGET)))
        frames = [self._plane() for _ in range(FLAT_MASTER_FRAMES)]
        _, tvar = fm.pair_temporal(frames[0], frames[1])
        mean_flat = np.mean(frames, axis=0)
        flat = fm.flat_result(mean_flat, self.black_dn, tvar, FLAT_MASTER_FRAMES)
        shade = fm.smooth_shading(mean_flat - self.black_dn)
        self._arrays["master_flat"] = (shade / max(float(shade.max()), 1e-9)).astype(np.float32)
        self._ref_exposure = ref
        span = self.full_scale - self.black_dn
        rates = [
            (p.mean_dn - self.black_dn) / p.exposure_us
            for p in points
            if 0.1 * span <= p.mean_dn - self.black_dn <= 0.7 * span
        ]
        self._ref_rate = statistics.median(rates) if rates else (FLAT_TARGET * span / ref)
        data: dict[str, Any] = {
            "response": resp.model_dump(mode="json", exclude={"points"}),
            "flat": flat.model_dump(mode="json"),
            "saturation_reached": clipped,
            "lens": self.lens,
            "reference_light_dn_per_us": self._ref_rate,
            "flicker_inflation": flicker,
            "min_exposure_us": e_lo,
        }
        if ptc is not None:
            data["photon_transfer"] = ptc.model_dump(mode="json")
            head = (
                f"system gain {ptc.system_gain_dn_per_e:.4f} DN/e-, "
                f"read noise {ptc.read_noise_e:.1f} e-, full well {ptc.full_well_e:.0f} e-, "
            )
        else:
            data["response"]["points"] = [p.model_dump(mode="json") for p in resp.points]
            peak = max((snr for _, snr in resp.snr), default=float("nan"))
            head = f"combined HDR output: peak SNR {peak:.0f}, "
        return self._record(
            FpResult(
                step="flat",
                status="pass",
                message=head + f"linearity error {resp.linearity_error_pct:.2f} %, "
                f"vignetting {flat.vignetting_corner_ratio:.2f}, PRNU {flat.prnu_pct:.2f} %"
                + ("" if clipped else " (saturation not reached: full well is a lower bound)"),
                data=data,
            )
        )

    def _dark_noise_at(self, gain_db: float, mode: str | None = None) -> float | None:
        """Read noise (DN) the darks measured at ``gain_db`` in gain ``mode`` (10 ms)."""
        dark = self.results.get("dark")
        if dark is None:
            return None
        pts = [
            p
            for p in dark.data.get("gain_points") or dark.data.get("points", [])
            if mode is None or p.get("gain_mode") in (None, mode)
        ]
        if not pts:
            return None
        best = min(
            pts,
            key=lambda p: (
                abs(p["gain_db"] - gain_db),
                abs(p["exposure_us"] - DARK_REF_EXPOSURE_US),
            ),
        )
        return float(best["read_noise_dn"]) if abs(best["gain_db"] - gain_db) < 1.0 else None

    def _gain_row(
        self, gain_db: float, exposure_us: float, mode: str | None = None
    ) -> dict[str, Any]:
        """Signal, temporal noise and the photon-transfer estimate at one gain: system gain
        k = shot variance / signal; read noise and full well in e- (the full well assumes
        the output clips at full scale, i.e. the ADC is the limit)."""
        self._set(gain_db=gain_db, exposure_us=exposure_us)
        sig, var = [], []
        for _ in range(NOISE_PAIRS):
            m, v = fm.pair_temporal(fm.centre(self._plane()), fm.centre(self._plane()))
            sig.append(m - self.black_dn)
            var.append(v)
        signal, v = statistics.median(sig), statistics.median(var)
        sigma = math.sqrt(v)
        rn = self._dark_noise_at(gain_db, mode)
        k = (v - (rn or 0.0) ** 2) / signal if signal > 0 else float("nan")
        ok = k > 0 and math.isfinite(k)
        return {
            "gain_db": round(gain_db, 2),
            "gain_mode": mode,
            "exposure_us": round(exposure_us, 1),
            "signal_dn": round(signal, 2),
            "sigma_dn": round(sigma, 3),
            "snr": round(signal / sigma, 1) if sigma > 0 else None,
            "read_noise_dn": round(rn, 3) if rn is not None else None,
            "gain_dn_per_e": round(k, 4) if ok else None,
            "read_noise_e": round(rn / k, 2) if ok and rn is not None else None,
            "full_well_e": round((self.full_scale - self.black_dn) / k) if ok else None,
        }

    def _gain_writable(self, gain_db: float) -> bool:
        """Whether ``gain_db`` takes in the current gain mode (the JAI's individual mode keeps
        Gain read-only: its per-sensor gains replace it)."""
        self._set(gain_db=gain_db)
        return abs(float(self._settings().gain_db) - gain_db) < 0.05

    def _gain_sweep(
        self, mode: str | None, base_exp: float, e_lo: float, e_hi: float, gains: list[float]
    ) -> tuple[list[dict[str, Any]], list[float]]:
        """Constant output over ``gains`` (exposure ÷ the gain): the rows, and the gains whose
        exposure would fall below the minimum (they continue in the low-light row)."""
        rows: list[dict[str, Any]] = []
        deferred: list[float] = []
        g0 = gains[0] if gains else 0.0
        for g in gains:
            exp = base_exp / 10 ** ((g - g0) / 20)
            if exp < e_lo:
                deferred.append(g)
                continue
            if not self._gain_writable(g):
                break
            rows.append(self._gain_row(g, min(e_hi, exp), mode))
        return rows, deferred

    def step_noise_gain(self) -> FpResult:
        """Every gain step on every camera that has gain (also where the design mode pins it),
        in each gain mode: SNR and the photon-transfer estimate per gain at a constant output;
        gain-mode switches; where each mode clips. Gains that would need less than the
        minimum exposure continue in the low-light row (Derek 2026-10-25: never cut)."""
        self._require("flat")
        if not self.gain_adjustable:
            return self._record(
                FpResult(step="noise_gain", status="skipped", message="the camera has no gain")
            )
        e_lo, e_hi = self._lit_range()
        base = self._ref_exposure * NOISE_TARGET / FLAT_TARGET
        modes = self._gain_modes()
        tables: dict[str, list[dict[str, Any]]] = {}
        self.deferred_gains = {}
        clips: dict[str, dict[str, Any]] = {}
        for mode in modes:
            if mode is not None:
                self._set(advanced={"prism.gain_mode": mode})
            rows, deferred = self._gain_sweep(mode, base, e_lo, e_hi, self._gain_steps())
            key = mode or "only"
            tables[key] = rows
            if deferred:
                self.deferred_gains[key] = deferred
            if mode is not None and rows:
                clips[key] = self._clip_level(rows[0], e_lo, e_hi)
        if modes[0] is not None:
            self._set(advanced={"prism.gain_mode": modes[0]})
        self._set(gain_db=self.mode_gain_db, exposure_us=self._ref_exposure)
        rows = tables[modes[0] or "only"]
        switches = [
            rows[i]["gain_db"]
            for i in range(1, len(rows))
            if rows[i]["snr"]
            and rows[i - 1]["snr"]
            and rows[i]["snr"] > GAIN_SWITCH_GAIN * rows[i - 1]["snr"]
        ]
        n_rows = sum(len(t) for t in tables.values())
        msg = f"{n_rows} gain steps measured" + (
            f" in {len(tables)} gain modes" if len(tables) > 1 else ""
        )
        if rows and rows[0]["full_well_e"]:
            msg += (
                f"; at {rows[0]['gain_db']:g} dB {rows[0]['gain_dn_per_e']} DN/e-, full well "
                f"≈ {rows[0]['full_well_e']} e-"
            )
        if switches:
            at = ", ".join(f"{s:g} dB" for s in switches)
            msg += f"; SNR rises at {at} (possible gain-mode switch)"
        summary = self._gain_mode_summary(tables, clips, modes)
        if summary:
            msg += f"; {summary['summary']}"
        if self.deferred_gains:
            msg += "; " + ", ".join(
                f"{len(v)} {k} steps continue in the low-light row"
                for k, v in self.deferred_gains.items()
            )
        return self._record(
            FpResult(
                step="noise_gain",
                status="pass",
                message=msg,
                data={
                    "table": rows,
                    "tables": tables,
                    "deferred": self.deferred_gains,
                    "snr_rises_at_db": switches,
                    "gain_modes": summary or None,
                },
            )
        )

    def _clip_level(self, row: dict[str, Any], e_lo: float, e_hi: float) -> dict[str, Any]:
        """Where a gain mode clips: exposure steps up to saturation from its first gain row."""
        self._set(gain_db=row["gain_db"])
        rate = row["signal_dn"] / max(row["exposure_us"], 1e-9)
        span = self.full_scale - self.black_dn
        top = 0.0
        for f in (0.9, 1.1, 1.4):
            self._set(exposure_us=min(e_hi, max(e_lo, f * span / max(rate, 1e-12))))
            top = max(top, float(np.mean(fm.centre(self._plane()))) - self.black_dn)
        k = row["gain_dn_per_e"]
        return {
            "clips_at_dn": round(top + self.black_dn, 1),
            "full_well_e": round(top / k) if k else None,
        }

    def _gain_mode_summary(
        self,
        tables: dict[str, list[dict[str, Any]]],
        clips: dict[str, dict[str, Any]],
        modes: list[str | None],
    ) -> dict[str, Any]:
        """Prism cameras: the two gain modes at their lowest gain, side by side."""
        if len(modes) < 2 or modes[0] is None or modes[1] is None:
            return {}
        a, b = modes[0], modes[1]
        ra, rb = tables.get(a) or [], tables.get(b) or []
        if not ra or not rb:
            return {}
        first_a, first_b = dict(ra[0]), dict(rb[0])
        first_a.update(clips.get(a, {}))
        first_b.update(clips.get(b, {}))
        ratio = (
            first_b["signal_dn"] / first_a["signal_dn"]
            if first_a["signal_dn"] > 0
            else float("nan")
        )
        return {
            a: first_a,
            b: first_b,
            "signal_ratio": round(ratio, 3),
            "summary": f"gain mode {b} at {first_b['gain_db']:g} dB gives {ratio:.2f}× the "
            f"signal of {a} at {first_a['gain_db']:g} dB; full well ≈ "
            f"{first_b.get('full_well_e')} e- (clips at {first_b.get('clips_at_dn', 0):.0f} DN) "
            f"vs ≈ {first_a.get('full_well_e')} e-",
        }
