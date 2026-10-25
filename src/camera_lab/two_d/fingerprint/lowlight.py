"""Low-light module: the diffuser with the iris fully closed (an end stop: repeatable) and
the bench light at its reference (Derek 2026-10-25: "are we testing in low light?"). It
measures what the cap and the bright sweep do not: signals from about one read noise up
to 2 % of the range, gain at a fixed exposure (does gain help when the light cannot
grow?), the gain steps the bright row could not reach, and N-frame averaging."""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from camera_lab.two_d.fingerprint import measure as fm
from camera_lab.two_d.fingerprint.common import (
    AVERAGE_FRAMES,
    LIGHT_TOLERANCE,
    LOW_LIGHT_GAIN_RN,
    LOW_LIGHT_LEVELS_RN,
    LOW_LIGHT_MAX,
    NOISE_TARGET,
    FpResult,
    iris_hint,
)
from camera_lab.two_d.fingerprint.core import CoreMeasurements


class LowLightMeasurements(CoreMeasurements):
    """The low-light module (it reuses the core module's gain helpers)."""

    def step_low_light(self) -> FpResult:
        self._require("flat", "dark")
        if self.lens_spec is None:  # pragma: no cover - a lens is picked before any row
            return self._record(FpResult(step="low_light", status="skipped", message="no lens"))
        closed = max(self.lens_spec.f_stops)
        ref = self._reference().f_number
        expected = self.lens_spec.relative(closed) / self.lens_spec.relative(ref)
        ratio, why = self._check_light(expected, LIGHT_TOLERANCE, f"f/{closed:g} (fully closed)")
        light = ratio * expected
        data: dict[str, Any] = {"f_number": closed, "light_vs_reference": light, "lens": self.lens}
        if why is not None:
            return self._record(
                FpResult(
                    step="low_light",
                    status="fail",
                    message=why + iris_hint(ratio, self.lens).replace(" ≈", "; ≈", 1),
                    data=data,
                )
            )
        rate = self._ref_rate * light  # DN per µs above black, here
        e_lo, e_hi = self._lit_range()
        span = self.full_scale - self.black_dn
        rn = float(self.results["dark"].data.get("read_noise_dn") or 1.0)
        ptc = self.results["flat"].data.get("photon_transfer") or {}
        k = ptc.get("system_gain_dn_per_e")
        self._set(gain_db=self._gain())

        levels = self._levels(rate, rn, k, e_lo, e_hi, span)
        fixed_exp = min(e_hi, max(e_lo, LOW_LIGHT_GAIN_RN * rn / max(rate, 1e-12)))
        gain_fixed = self._gain_at_fixed_exposure(fixed_exp)
        deferred = self._deferred_rows(rate, span, e_lo, e_hi)
        averaging = self._averaging(fixed_exp)
        self._set(gain_db=self.mode_gain_db, exposure_us=self._ref_exposure)
        data |= {
            "rate_dn_per_us": rate,
            "levels": levels,
            "gain_fixed_exposure": {"exposure_us": fixed_exp, "rows": gain_fixed},
            "deferred_gain_rows": deferred,
            "averaging": averaging,
        }
        msg = f"{self.lens}: light {light:.4f}× fully open"
        if levels:
            lo = levels[0]
            msg += (
                f"; {len(levels)} levels from {lo['signal_dn']:.1f} DN (SNR {lo['snr']}, model "
                f"{lo['model_snr']}) to {levels[-1]['signal_dn']:.0f} DN"
            )
        if gain_fixed:
            first = gain_fixed[0]
            best = max(gain_fixed, key=lambda r: r["snr"] or 0)
            msg += (
                f"; fixed exposure {fixed_exp / 1000:g} ms: SNR {first['snr']} at "
                f"{first['gain_db']:g} dB, best {best['snr']} at {best['gain_db']:g} dB"
                + (f" ({best['gain_mode']})" if best.get("gain_mode") else "")
            )
        if deferred:
            msg += f"; {len(deferred)} high-gain steps measured here"
        if averaging:
            last = averaging[-1]
            msg += (
                f"; averaging {last['frames']} frames: ×{last['snr_gain']:.2f} "
                f"(√N {last['sqrt_n']:.2f})"
            )
        return self._record(FpResult(step="low_light", status="pass", message=msg, data=data))

    def _levels(
        self, rate: float, rn: float, k: float | None, e_lo: float, e_hi: float, span: float
    ) -> list[dict[str, Any]]:
        """Signals from about one read noise up to LOW_LIGHT_MAX of the range."""
        out: list[dict[str, Any]] = []
        for n_rn in LOW_LIGHT_LEVELS_RN:
            target = n_rn * rn
            if target > LOW_LIGHT_MAX * span:
                break
            exp = target / max(rate, 1e-12)
            if exp < e_lo or exp > e_hi:
                continue
            self._set(exposure_us=exp)
            a, b = self._plane(), self._plane()
            mean, var = fm.pair_temporal(fm.centre(a), fm.centre(b))
            signal = mean - self.black_dn
            sigma = math.sqrt(var)
            low = float(np.min(a))
            model = signal / math.sqrt(rn**2 + k * signal) if k and signal > 0 else None
            centre = fm.centre(a)
            out.append(
                {
                    "exposure_us": round(exp, 1),
                    "signal_dn": round(signal, 3),
                    "sigma_dn": round(sigma, 3),
                    "snr": round(signal / sigma, 2) if sigma > 0 else None,
                    "model_snr": round(model, 2) if model is not None else None,
                    # at black: pixels stuck at the lowest code (the black clipping)
                    "at_lowest_code_pct": round(float(np.mean(a == low)) * 100, 3),
                    "distinct_values": int(np.unique(np.round(centre)).size),
                }
            )
        return out

    def _gain_at_fixed_exposure(self, exposure_us: float) -> list[dict[str, Any]]:
        """Every gain step at one exposure, in each gain mode: the light cannot grow."""
        rows: list[dict[str, Any]] = []
        modes = self._gain_modes()
        for mode in modes:
            if mode is not None:
                self._set(advanced={"prism.gain_mode": mode})
            for g in self._gain_steps():
                if not self._gain_writable(g):
                    break
                row = self._gain_row(g, exposure_us, mode)
                if row["signal_dn"] > 0.9 * (self.full_scale - self.black_dn):
                    break  # clipped: more gain only clips more
                rows.append(row)
        if modes[0] is not None:
            self._set(advanced={"prism.gain_mode": modes[0]})
        return rows

    def _deferred_rows(
        self, rate: float, span: float, e_lo: float, e_hi: float
    ) -> list[dict[str, Any]]:
        """The constant-output gain steps the bright row could not reach (exposure below the
        minimum there), at the same output level, with the light this low."""
        rows: list[dict[str, Any]] = []
        for key, gains in (self.deferred_gains or {}).items():
            mode = None if key == "only" else key
            if mode is not None:
                self._set(advanced={"prism.gain_mode": mode})
            steps = self._gain_steps()
            g0 = steps[0] if steps else 0.0
            base = NOISE_TARGET * span / max(rate, 1e-12)  # at the mode's lowest gain
            for g in gains:
                exp = base / 10 ** ((g - g0) / 20)
                if exp < e_lo or exp > e_hi or not self._gain_writable(g):
                    continue
                rows.append({**self._gain_row(g, exp, mode), "row": "low light"})
        modes = self._gain_modes()
        if modes[0] is not None:
            self._set(advanced={"prism.gain_mode": modes[0]})
        return rows

    def _averaging(self, exposure_us: float) -> list[dict[str, Any]]:
        """Temporal noise of the mean of N frames (two independent stacks per N): √N."""
        self._set(exposure_us=exposure_us)
        out: list[dict[str, Any]] = []
        sigma1 = None
        for n in AVERAGE_FRAMES:
            a = np.mean([fm.centre(self._plane()) for _ in range(n)], axis=0)
            b = np.mean([fm.centre(self._plane()) for _ in range(n)], axis=0)
            _, var = fm.pair_temporal(a, b)
            sigma = math.sqrt(var)
            sigma1 = sigma1 or sigma
            out.append(
                {
                    "frames": n,
                    "sigma_dn": round(sigma, 4),
                    "snr_gain": round(sigma1 / sigma, 3) if sigma > 0 else None,
                    "sqrt_n": round(math.sqrt(n), 3),
                }
            )
        return out
