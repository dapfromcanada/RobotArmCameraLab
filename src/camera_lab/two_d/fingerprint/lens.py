"""Lens module: the diffuser at each marked stop between the lens's end stops (one row per
stop, Derek 2026-10-25). Each row measures the light the lens passes there against fully
open, and vignetting and per-colour corner shading (prism cameras are rated for one
aperture: "F4 prism optical system" on the JAI AP-3200T). Lens- and camera-dependent: the
results go to the catalog's lens stops and camera + lens pair."""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from camera_lab.two_d.fingerprint import measure as fm
from camera_lab.two_d.fingerprint.base import SessionBase
from camera_lab.two_d.fingerprint.common import (
    COLOUR_FRAMES,
    FLAT_MASTER_LEVEL,
    LENS_STEPS,
    LIGHT_TOLERANCE,
    FpResult,
    FpStep,
    iris_hint,
)


class LensMeasurements(SessionBase):
    """The lens module."""

    def _expected_at(self, f_number: float) -> float:
        """Light at ``f_number`` relative to the reference iris (measured transmission where
        the catalog has it, else the nominal (f_ref / f)²)."""
        ref = self._reference().f_number
        if self.lens_spec is None:
            return (ref / f_number) ** 2
        return self.lens_spec.relative(f_number) / self.lens_spec.relative(ref)

    def _stop_tolerance(self, f_number: float) -> float:
        """How far the light may be off and still be this stop: halfway (geometrically) to
        the neighbouring stops, so the next stop can never pass for this one (the Kowa's
        f/1.4 → f/2 is only 1.35×: a fixed ±35 % let a fully open iris pass for f/2)."""
        if self.lens_spec is None:
            return LIGHT_TOLERANCE
        marks = sorted(self.lens_spec.f_stops)
        here = self._expected_at(f_number)
        steps = [
            max(here, other) / min(here, other)
            for f in marks
            if f != f_number and (other := self._expected_at(f)) > 0
        ]
        nearest = min(steps, default=LIGHT_TOLERANCE**2)
        return float(min(LIGHT_TOLERANCE, math.sqrt(nearest)))

    def step_lens_iris(self, index: int) -> FpResult:
        """Diffuser on, iris at the row's marked stop."""
        step: FpStep = LENS_STEPS[index]
        self._require("flat")
        plan = next((p for p in self.plan.values() if step in p.measures), None)
        f = plan.f_number if plan is not None else None
        if f is None:
            return self._record(FpResult(step=step, status="skipped", message="no stop"))
        expected = self._expected_at(f)
        ratio, why = self._check_light(expected, self._stop_tolerance(f), f"f/{f:g}")
        light = ratio * expected
        data: dict[str, Any] = {"f_number": f, "light_vs_reference": light, "lens": self.lens}
        if why is not None:
            return self._record(
                FpResult(
                    step=step,
                    status="fail",
                    message=why + iris_hint(ratio, self.lens).replace(" ≈", "; ≈", 1),
                    data=data,
                )
            )
        data |= self._shading(light)
        flat = self.results["flat"].data.get("flat", {})
        colour = self.results.get("colour")
        before = colour.data if colour is not None and colour.status == "pass" else {}
        msg = f"{self.lens}: light {light:.3f}× fully open (nominal {expected:.3f}); vignetting "
        open_vig = flat.get("vignetting_corner_ratio", 0)
        msg += f"{data['vignetting_corner_ratio']:.2f} (fully open {open_vig:.2f})"
        if "corner_colour_cast_pct" in data:
            was = before.get("corner_colour_cast_pct")
            msg += f"; colour cast at the corners {data['corner_colour_cast_pct']:.1f} %" + (
                f" (fully open {was:.1f} %)" if was is not None else ""
            )
        return self._record(FpResult(step=step, status="pass", message=msg, data=data))

    def _shading(self, light: float) -> dict[str, Any]:
        """Vignetting and, for colour sensors, per-plane corner ratios at the light now."""
        e_lo, e_hi = self._lit_range()
        span = self.full_scale - self.black_dn
        exp = FLAT_MASTER_LEVEL * span / max(self._ref_rate * light, 1e-12)
        self._set(exposure_us=min(e_hi, max(e_lo, exp)))
        frames = [self._frame() for _ in range(COLOUR_FRAMES)]
        try:
            res = fm.colour_result(frames, self.fmt, self.black_dn)
        except ValueError:  # mono: the one plane
            mean = np.mean([fm.raw_plane(f, self.fmt) for f in frames], axis=0)
            vig = fm.flat_result(mean, self.black_dn, 0.0, len(frames)).vignetting_corner_ratio
            return {"vignetting_corner_ratio": vig}
        green = [p.corner_ratio for p in res.planes if p.name.startswith("G")]
        return {
            "vignetting_corner_ratio": float(np.mean(green))
            if green
            else res.planes[0].corner_ratio,
            "corner_ratio": {p.name: p.corner_ratio for p in res.planes},
            "corner_colour_cast_pct": res.corner_colour_cast_pct,
            "relative_to_green": {p.name: p.relative_to_green for p in res.planes},
        }
