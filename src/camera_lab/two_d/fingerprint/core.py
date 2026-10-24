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
    FLAT_MASTER_FRAMES,
    FLAT_MASTER_LEVEL,
    FLAT_SWEEP,
    FLAT_TARGET,
    GAIN_SWITCH_GAIN,
    MAX_EXPOSURE_US,
    NOISE_GAIN_STEP_DB,
    NOISE_PAIRS,
    NOISE_TARGET,
    PERSIST_SLOT,
    PIPELINE_FRAMES,
    SETTLE_FRAMES,
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
        if black is not None and not isinstance(black, NotAdjustable):
            target = min(BLACK_RAISE, float(black.max.value))
            if isinstance(current, int | float) and current < target:
                self._set(advanced={"black.level": target})
                black_raised = target
        self.full_scale = float((1 << PIXEL_FORMAT_BITS[self.fmt]) - 1)
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
        exp = float(self._settings().exposure_us)
        m0 = float(np.mean(self._plane()))
        new = min(exp * 2, hi) if exp * 2 <= hi else max(exp / 2, lo)
        self._set(exposure_us=new)
        means = [float(np.mean(self._plane())) for _ in range(SETTLE_FRAMES)]
        self._set(exposure_us=exp)
        change = means[-1] - m0
        stale = sum(1 for m in means if abs(change) > 0 and (m - m0) / change < 0.5)
        pipe = self._pipeline_timing()
        data = {
            "fps_full_frame": fps,
            "stale_frames_after_change": stale,
            "settle_test_level_change_dn": change,
            "pipeline": pipe,
        }
        note = "" if abs(change) > 0.02 * self.full_scale else " (scene too dark to test settling)"
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

    def step_dark(self) -> FpResult:
        """The human has put the lens cap on."""
        self._require("identity")
        g_lo, g_hi = self._range("gain_db")
        if not self.gain_knob:
            g_lo = g_hi = self._gain()
        e_lo, e_hi = self._range("exposure_us")
        self._set(gain_db=g_lo, exposure_us=min(max(DARK_EXPOSURES_US[-1], e_lo), e_hi))
        if not fm.looks_dark(self._plane(), self.full_scale):
            return self._record(
                FpResult(
                    step="dark",
                    status="fail",
                    message="light reaches the sensor: put the lens cap on",
                )
            )
        points = []
        long_mean = None
        for exp in DARK_EXPOSURES_US:
            exp = min(max(exp, e_lo), e_hi)
            self._set(gain_db=g_lo, exposure_us=exp)
            frames = [self._plane() for _ in range(DARK_FRAMES)]
            points.append(fm.dark_point(frames, g_lo, exp))
            mean = np.mean(frames, axis=0)
            if exp == min(max(DARK_EXPOSURES_US[1], e_lo), e_hi):
                self._arrays["master_dark"] = mean.astype(np.float32)
                self._row_noise_dn2["own"] = fm.row_variance(frames[0], frames[1])
            long_mean = mean
        g = g_lo + DARK_GAIN_STEP_DB
        while self.gain_knob and g <= g_hi + 1e-6:
            self._set(gain_db=g, exposure_us=min(max(DARK_EXPOSURES_US[1], e_lo), e_hi))
            points.append(
                fm.dark_point([self._plane() for _ in range(DARK_FRAMES)], g, DARK_EXPOSURES_US[1])
            )
            g += DARK_GAIN_STEP_DB
        channels: dict[str, Any] = {}
        if self.hdr_channels:
            exp = min(max(DARK_EXPOSURES_US[1], e_lo), e_hi)
            self._set(exposure_us=exp)
            for out in self.hdr_channels:
                self._output(out, self.channel_fmt)
                frames = [self._plane() for _ in range(DARK_FRAMES)]
                pt = fm.dark_point(frames, g_lo, exp)
                self._channel_black[out] = pt
                self._row_noise_dn2[out] = fm.row_variance(frames[0], frames[1])
                channels[out] = {
                    **pt.model_dump(mode="json"),
                    "row_noise_dn": math.sqrt(self._row_noise_dn2[out]),
                }
            self._output("HDR", self.fmt)
        self._set(exposure_us=self._view_exposure)  # viewable again
        if self.gain_knob:
            self._set(gain_db=self._view_gain)
        if long_mean is None:  # pragma: no cover - DARK_EXPOSURES_US is never empty
            raise RuntimeError("no dark frames")
        res = fm.dark_result(points, long_mean)
        self.black_dn = res.black_dn
        data = res.model_dump(mode="json", exclude={"hot_pixels"})
        data["hot_pixels_first"] = [list(p) for p in res.hot_pixels[:50]]
        data["row_noise_dn"] = math.sqrt(self._row_noise_dn2.get("own", 0.0))
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
        return self._record(
            FpResult(
                step="dark",
                status="pass",
                message=f"black {res.black_dn:.1f} DN, read noise {res.read_noise_dn:.2f} DN, "
                f"dark current {res.dark_current_dn_s:.1f} DN/s, {res.hot_pixel_count} hot pixels"
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
                ptc = fm.photon_transfer(points, self.black_dn)
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

    def step_noise_gain(self) -> FpResult:
        self._require("flat")
        if not self.gain_knob:
            return self._record(
                FpResult(
                    step="noise_gain",
                    status="skipped",
                    message=f"gain is not a knob of mode {self.mode}",
                )
            )
        g_lo, g_hi = self._range("gain_db")
        e_lo, e_hi = self._lit_range()
        base = self._ref_exposure * NOISE_TARGET / FLAT_TARGET
        rows: list[dict[str, Any]] = []
        g = g_lo
        while g <= g_hi + 1e-6:
            exp = base / 10 ** ((g - g_lo) / 20)
            if exp < e_lo:
                break
            self._set(gain_db=g, exposure_us=min(e_hi, exp))
            sig, var = [], []
            for _ in range(NOISE_PAIRS):
                m, v = fm.pair_temporal(fm.centre(self._plane()), fm.centre(self._plane()))
                sig.append(m - self.black_dn)
                var.append(v)
            signal, sigma = statistics.median(sig), math.sqrt(statistics.median(var))
            rows.append(
                {
                    "gain_db": round(g, 2),
                    "exposure_us": round(exp, 1),
                    "signal_dn": round(signal, 2),
                    "sigma_dn": round(sigma, 3),
                    "snr": round(signal / sigma, 1) if sigma > 0 else None,
                }
            )
            g += NOISE_GAIN_STEP_DB
        self._set(gain_db=g_lo)
        switches = [
            rows[i]["gain_db"]
            for i in range(1, len(rows))
            if rows[i]["snr"]
            and rows[i - 1]["snr"]
            and rows[i]["snr"] > GAIN_SWITCH_GAIN * rows[i - 1]["snr"]
        ]
        msg = f"{len(rows)} gain steps measured"
        if switches:
            at = ", ".join(f"{s:g} dB" for s in switches)
            msg += f"; SNR rises at {at} (possible gain-mode switch)"
        return self._record(
            FpResult(
                step="noise_gain",
                status="pass",
                message=msg,
                data={"table": rows, "snr_rises_at_db": switches},
            )
        )

    def step_user_sets(self) -> FpResult:
        """What survives a power cycle: marker values → User Set 2 (boot set) → software
        reboot (identical to a power cycle, verified 2026-10-24) → kept / lost per setting.
        The boot set is put back afterwards; the report restores the settings."""
        self._require("identity")
        info = self.service.user_set_info(self.camera_id)
        slots = info.get("slots", [])
        if not info.get("boot_set_feature") or PERSIST_SLOT not in slots:
            return self._record(
                FpResult(
                    step="user_sets",
                    status="pass",
                    message=f"user sets {', '.join(slots) or 'none'}; no boot-set feature or no "
                    f"{PERSIST_SLOT}: persistence not tested (power-cycle test by a human)",
                    data=info,
                )
            )
        view = self.service.get_settings(self.camera_id)
        s0 = view.settings
        marks: dict[str, Any] = {}
        lo, hi = self._range("exposure_us")
        marks["exposure_us"] = min(hi, max(lo, s0.exposure_us * 0.83))
        g = view.ranges.get("gain_db")
        if g is not None and not isinstance(g, NotAdjustable) and g.max.value >= 3.3:
            marks["gain_db"] = 3.3
        if s0.white_balance:
            marks["white_balance"] = {**s0.white_balance, "red": 1.23}
        others = [
            f
            for f in view.options.get("pixel_format", ())
            if format_family(f) == format_family(s0.pixel_format) and f != s0.pixel_format
        ]
        if others:
            marks["pixel_format"] = others[0]
        adv: dict[str, Any] = {}
        if "black.level" in view.ranges:
            adv["black.level"] = 2.3
        for key in ("lut.enabled", "color.transformation"):
            if key in s0.advanced:
                adv[key] = "on" if s0.advanced[key] == "off" else "off"
        if adv:
            marks["advanced"] = adv
        self._set(**marks)
        if "hdr.enabled" in s0.advanced:  # last: it locks exposure and gain on some cameras
            self._set(
                advanced={"hdr.enabled": "on" if s0.advanced["hdr.enabled"] == "off" else "off"}
            )
        marked = self._settings()
        prev_boot = info.get("boot_set") or "Default"
        self.service.save_user_set(
            self.camera_id, PERSIST_SLOT, confirm=True, make_default=True, actor=self.actor
        )
        try:
            restart = self.service.reboot_camera(self.camera_id, confirm=True, actor=self.actor)
            after = self._settings()
        finally:
            self.service.set_boot_user_set(
                self.camera_id, prev_boot, confirm=True, actor=self.actor
            )
        kept: list[str] = []
        lost: list[str] = []
        pairs: list[tuple[str, Any, Any]] = [
            (k, getattr(marked, k), getattr(after, k))
            for k in ("exposure_us", "gain_db", "pixel_format")
            if k in marks
        ]
        if "white_balance" in marks:
            pairs.append(
                (
                    "white_balance.red",
                    (marked.white_balance or {}).get("red"),
                    (after.white_balance or {}).get("red"),
                )
            )
        for key in (*adv, "hdr.enabled"):
            if key in marked.advanced:
                pairs.append((key, marked.advanced.get(key), after.advanced.get(key)))
        for name, want, got in pairs:
            same = (
                abs(float(got) - float(want)) <= 0.01 * max(abs(float(want)), 1e-9)
                if isinstance(want, int | float) and isinstance(got, int | float)
                else got == want
            )
            (kept if same else lost).append(name)
        verified = restart.get("restart_verified")
        return self._record(
            FpResult(
                step="user_sets",
                status="pass",
                message=f"after a reboot ({restart['verified_by']}"
                + ("" if verified is not False else ", NOT verified")
                + f") from {PERSIST_SLOT}: kept {', '.join(kept) or 'nothing'}; lost "
                f"{', '.join(lost) or 'nothing'}; boot set back to {prev_boot}",
                data={
                    **info,
                    "persistence": {"kept": kept, "lost": lost, "slot": PERSIST_SLOT},
                    "restart": restart,
                },
            )
        )
