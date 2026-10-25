"""Lens module: the diffuser at a smaller iris (the lens row). Vignetting and colour
shading change with the iris; prism cameras are rated for one aperture ("F4 prism optical
system" on the JAI AP-3200T), so this row shows what opening or closing the iris does.
Lens- and installation-dependent: recorded, not part of the camera's numbers."""

from __future__ import annotations

from typing import Any

import numpy as np

from camera_lab.two_d.fingerprint import measure as fm
from camera_lab.two_d.fingerprint.base import SessionBase
from camera_lab.two_d.fingerprint.common import (
    COLOUR_FRAMES,
    FLAT_MASTER_LEVEL,
    LENS_STOPS_CLOSED,
    LIGHT_TOLERANCE,
    FpResult,
    iris_hint,
)


class LensMeasurements(SessionBase):
    """The lens module."""

    def step_lens_iris(self) -> FpResult:
        """Diffuser on, iris closed LENS_STOPS_CLOSED stops from the diffuser row."""
        self._require("flat")
        expected = 0.5**LENS_STOPS_CLOSED
        ratio, why = self._check_light(
            expected, LIGHT_TOLERANCE, f"{LENS_STOPS_CLOSED} f-stops less than the diffuser row"
        )
        data: dict[str, Any] = {"light_vs_diffuser_row": ratio * expected, "lens": self.lens}
        if why is not None:
            return self._record(
                FpResult(
                    step="lens_iris",
                    status="fail",
                    message=why + iris_hint(ratio, self.lens).replace(" ≈", "; ≈", 1),
                    data=data,
                )
            )
        e_lo, e_hi = self._lit_range()
        span = self.full_scale - self.black_dn
        exp = FLAT_MASTER_LEVEL * span / max(self._ref_rate * expected * ratio, 1e-12)
        self._set(exposure_us=min(e_hi, max(e_lo, exp)))
        frames = [self._frame() for _ in range(COLOUR_FRAMES)]
        colour = self.results.get("colour")
        before = colour.data if colour is not None and colour.status == "pass" else {}
        flat = self.results["flat"].data.get("flat", {})
        try:
            res = fm.colour_result(frames, self.fmt, self.black_dn)
        except ValueError:  # mono: the one plane
            mean = np.mean([fm.raw_plane(f, self.fmt) for f in frames], axis=0)
            vig = fm.flat_result(mean, self.black_dn, 0.0, len(frames)).vignetting_corner_ratio
            data |= {"vignetting_corner_ratio": vig}
            msg = (
                f"vignetting {vig:.2f} here vs {flat.get('vignetting_corner_ratio', 0):.2f} in "
                "the diffuser row"
            )
        else:
            green = [p.corner_ratio for p in res.planes if p.name.startswith("G")]
            vig = float(np.mean(green)) if green else res.planes[0].corner_ratio
            cast = res.corner_colour_cast_pct
            data |= {
                "vignetting_corner_ratio": vig,
                "corner_ratio": {p.name: p.corner_ratio for p in res.planes},
                "corner_colour_cast_pct": cast,
                "relative_to_green": {p.name: p.relative_to_green for p in res.planes},
            }
            was_vig = flat.get("vignetting_corner_ratio")
            was_cast = before.get("corner_colour_cast_pct")
            msg = f"vignetting {vig:.2f}" + (f" (diffuser row {was_vig:.2f})" if was_vig else "")
            msg += f"; colour cast at the corners {cast:.1f} %" + (
                f" (diffuser row {was_cast:.1f} %)" if was_cast is not None else ""
            )
        return self._record(
            FpResult(
                step="lens_iris",
                status="pass",
                message=f"{self.lens or 'iris'}: light {ratio * expected:.2f}× the diffuser row; "
                + msg,
                data=data,
            )
        )
