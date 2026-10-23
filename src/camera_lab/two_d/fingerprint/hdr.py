"""HDR module: each HDR channel's photon transfer at the recipe's light, the light
checks before each stage and after the last one."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

import numpy as np

from camera_lab.two_d.fingerprint import measure as fm

if TYPE_CHECKING:
    pass
from camera_lab.two_d.fingerprint.base import SessionBase
from camera_lab.two_d.fingerprint.common import (
    CHANNEL_PREFIX,
    CHANNEL_REPEAT_KEYS,
    FLAT_SWEEP,
    FLAT_TARGET,
    LIGHT_BACK_TOLERANCE,
    LIGHT_TOLERANCE,
    PROBE_FRACTION,
    REPEATS,
    STAGE_STEPS,
    SWEEP_LOW_FACTOR,
    FpResult,
    iris_hint,
    repeat_note,
    repeat_summary,
)


class HdrMeasurements(SessionBase):
    """The HDR module."""

    def _light_rate(self, expected: float) -> float:
        """Signal per µs above black on the camera's own output (linear) at the light now.

        Starts at the exposure the expected light needs for ~30 % and steps by 8× if the
        frame is clipped or nearly black.
        """
        if self.hdr_channels:
            self._output("HDR", self.fmt)
        e_lo, e_hi = self._lit_range()
        span = self.full_scale - self.black_dn
        exp = min(e_hi, max(e_lo, PROBE_FRACTION * span / max(self._ref_rate * expected, 1e-12)))
        m = 0.0
        for _ in range(6):
            self._set(exposure_us=exp)
            m = float(np.mean(fm.centre(self._plane()))) - self.black_dn
            if m >= 0.9 * span and exp > e_lo:
                exp = max(e_lo, exp / 8)
            elif m < 0.02 * span and exp < e_hi:
                exp = min(e_hi, exp * 8)
            else:
                break
        return max(m, 0.0) / exp

    def _check_light(
        self, expected: float, tolerance: float, what: str
    ) -> tuple[float, str | None]:
        """(measured / expected light, a STOP message or None)."""
        rate = self._light_rate(expected)
        ratio = rate / max(self._ref_rate * expected, 1e-12)
        if 1 / tolerance <= ratio <= tolerance:
            return ratio, None
        if ratio <= 0:
            how = "no light reaches the camera"
        else:
            how = f"the light is {ratio:.2f}× what {what} should give"
        return ratio, f"{how}: check the dimmer and the iris, then Retry"

    def _measure_channel(self, out: str, e_lo: float, e_hi: float) -> str | None:
        """Photon transfer of one HDR channel REPEATS times; a problem text or None."""
        runs: list[dict[str, Any]] = []
        for _ in range(REPEATS):
            why = self._measure_channel_once(out, e_lo, e_hi)
            if why is not None:
                self._channels.pop(out, None)
                return why
            runs.append(self._channels[out])
        summary = repeat_summary([r["photon_transfer"] for r in runs], CHANNEL_REPEAT_KEYS)
        self._channels[out] = {**runs[-1], "repeats": summary, "repeat_note": repeat_note(summary)}
        return None

    def _measure_channel_once(self, out: str, e_lo: float, e_hi: float) -> str | None:
        """Photon transfer of one HDR channel at the light now; a problem text or None."""
        full = self._output(out, self.channel_fmt)
        black = self._channel_black[out].black_dn
        self._set(exposure_us=e_lo)
        if float(np.mean(fm.centre(self._plane()))) >= 0.98 * full:
            return (
                f"saturated even at the shortest exposure ({e_lo / 1000:g} ms): needs more than "
                f"{SWEEP_LOW_FACTOR:.0f}× less light" + iris_hint(SWEEP_LOW_FACTOR, self.lens)
            )
        ref = self._exposure_for(FLAT_TARGET, e_lo, e_hi, full_scale=full, black=black)
        if ref is None:
            if self._last_fraction >= FLAT_TARGET:  # still too bright at e_lo
                factor = SWEEP_LOW_FACTOR * self._last_fraction / FLAT_TARGET
                return f"too bright: needs about {factor:.0f}× less light" + iris_hint(
                    factor, self.lens
                )
            more = FLAT_TARGET / max(self._last_fraction, 1e-3)
            return (
                f"too dark at the longest exposure ({e_hi / 1000:.0f} ms): needs about "
                f"{more:.1f}× more light" + iris_hint(1 / more, self.lens)
            )
        if ref < SWEEP_LOW_FACTOR * e_lo:
            factor = SWEEP_LOW_FACTOR * e_lo / ref
            return (
                f"too bright for a full sweep (80 % at {ref / 1000:.2f} ms, shortest "
                f"{e_lo / 1000:g} ms): "
                f"needs about {factor:.1f}× less light" + iris_hint(factor, self.lens)
            )
        points: dict[float, fm.PtcPoint] = {}
        for k in FLAT_SWEEP:
            exp = min(e_hi, max(e_lo, ref * k))
            if exp in points:
                continue
            self._set(exposure_us=exp)
            points[exp] = self._pair_point(exp)
        flicker, why = self._flicker_problem(list(points.values()), out)
        if why is not None:
            return why
        try:
            ptc = fm.photon_transfer(list(points.values()), black)
        except ValueError as exc:
            return str(exc)
        self._channels[out] = {
            "photon_transfer": ptc.model_dump(mode="json"),
            "snr": [list(r) for r in fm.snr_table(ptc.points, black, ptc.saturation_dn)],
            "read_noise_dn": self._channel_black[out].read_noise_dn,
            "reference_exposure_us": ref,
            "flicker_inflation": flicker,
            "row_noise_dn": math.sqrt(self._row_noise_dn2.get(out, 0.0)),
            "lens": self.lens,
        }
        return None

    def step_stage(self, idx: int) -> FpResult:
        """HDR channel stage ``idx`` (diffuser on, light as the plan says)."""
        step = STAGE_STEPS[idx]
        self._require("flat")
        used = (
            idx < len(self.recipe.channel_stages)
            if self.recipe is not None
            else idx == 0 and self.hdr_capable
        )
        if not used or not self.hdr_channels:
            reason = "not an HDR camera" if not self.hdr_channels else "not used"
            return self._record(FpResult(step=step, status="skipped", message=reason))
        light: dict[str, Any] = {}
        if self.recipe is not None:
            st = self.recipe.channel_stages[idx]
            unknown = [c for c in st.channels if c not in self.hdr_channels]
            if unknown:
                return self._record(
                    FpResult(
                        step=step,
                        status="fail",
                        message=f"the recipe names channels this camera does not have: "
                        f"{', '.join(unknown)} (camera: {', '.join(self.hdr_channels)})",
                    )
                )
            self.lens = self._lens_at(st)
            expected = self._expected(st)
            ratio, why = self._check_light(
                expected, LIGHT_TOLERANCE, f"dimmer {st.dimmer}, f/{st.f_number:g}"
            )
            light = {
                "dimmer": st.dimmer,
                "f_number": st.f_number,
                "expected_vs_reference": expected,
                "measured_vs_expected": ratio,
            }
            if why is not None:
                self._set(exposure_us=self._ref_exposure)
                return self._record(
                    FpResult(step=step, status="fail", message=why, data={"light": light})
                )
            channels: tuple[str, ...] = st.channels
        else:
            channels = self.hdr_channels
        e_lo, e_hi = self._lit_range()
        problems: dict[str, str] = {}
        try:
            for out in channels:
                if out not in self._channels:
                    why = self._measure_channel(out, e_lo, e_hi)
                    if why is not None:
                        problems[out] = why
        finally:
            self._output("HDR", self.fmt)
            if self._ref_exposure:
                self._set(exposure_us=self._ref_exposure)
        measured = {c: self._channels[c] for c in channels if c in self._channels}
        done = ", ".join(
            f"{k.removeprefix(CHANNEL_PREFIX)}: "
            f"{v['photon_transfer']['system_gain_dn_per_e']:.3f} DN/e-"
            f" {v['photon_transfer']['full_well_e']:.0f} e-{v.get('repeat_note', '')}"
            for k, v in measured.items()
        )
        data = {"channels": measured, "problems": problems, "light": light, "lens": self.lens}
        if problems:
            todo = "; ".join(f"{k}: {v}" for k, v in problems.items())
            if self.recipe is not None:
                tail = (
                    ". The light matches the recipe, so the RECIPE is off for this camera: "
                    "do not change the light yourself — copy this message to the engineer "
                    "(camera profile, fingerprint: channel_stages)"
                )
            else:
                tail = ". Change the light as asked, update the Lens / iris box, then Retry"
            return self._record(
                FpResult(
                    step=step,
                    status="fail",
                    message=todo + tail + (f" (kept {done})" if done else ""),
                    data=data,
                )
            )
        return self._record(
            FpResult(step=step, status="pass", message=f"channels {done}", data=data)
        )

    def step_light_back(self) -> FpResult:
        """The bench is back at the reference light (iris fully open): verified."""
        self._require("flat", *(m for m in self._planned() if m in STAGE_STEPS))
        if not self._light_changes or not self._channels:
            return self._record(
                FpResult(step="light_back", status="skipped", message="the light never changed")
            )
        if self.recipe is not None:
            self.lens = self._lens_at(self.recipe.reference)
        ratio, why = self._check_light(1.0, LIGHT_BACK_TOLERANCE, "the reference (flat step)")
        self._set(exposure_us=self._ref_exposure)
        data = {"measured_vs_reference": ratio, "lens": self.lens}
        if why is not None:
            return self._record(FpResult(step="light_back", status="fail", message=why, data=data))
        return self._record(
            FpResult(
                step="light_back",
                status="pass",
                message=f"light back at the reference ({ratio:.2f}× the flat step)",
                data=data,
            )
        )
