"""Colour module (Bayer and prism cameras): each raw colour plane on the diffuser —
response relative to green, temporal noise, PRNU, and the Bayer green imbalance (Gr vs
Gb). Measured on the raw sites / sensors, never on demosaiced data."""

from __future__ import annotations

from typing import Any

from camera_lab.two_d.fingerprint import measure as fm
from camera_lab.two_d.fingerprint.base import SessionBase
from camera_lab.two_d.fingerprint.common import (
    COLOUR_FRAMES,
    FLAT_MASTER_LEVEL,
    FLAT_TARGET,
    FpResult,
    format_family,
)


class ColourMeasurements(SessionBase):
    """The colour module."""

    def step_colour(self) -> FpResult:
        """Diffuser on, light at the reference: at the flat master level (50 %)."""
        self._require("flat")
        if self.colour == "mono" or format_family(self.fmt) == "Mono":
            return self._record(
                FpResult(step="colour", status="skipped", message="mono sensor: no colour planes")
            )
        e_lo, e_hi = self._lit_range()
        target = self._ref_exposure * FLAT_MASTER_LEVEL / FLAT_TARGET
        exp = min(e_hi, max(e_lo, target))
        self._set(exposure_us=exp)
        frames = [self._frame() for _ in range(COLOUR_FRAMES)]
        try:
            res = fm.colour_result(frames, self.fmt, self.black_dn)
        except ValueError as exc:
            return self._record(FpResult(step="colour", status="skipped", message=str(exc)))
        planes = {p.name: p for p in res.planes}
        data: dict[str, Any] = {
            "exposure_us": exp,
            "planes": [p.model_dump(mode="json") for p in res.planes],
            "relative_to_green": {k: p.relative_to_green for k, p in planes.items()},
            "prnu_pct": {k: p.prnu_pct for k, p in planes.items()},
            "snr": {k: p.snr for k, p in planes.items()},
            "green_imbalance_pct": res.green_imbalance_pct,
            "corner_ratio": {k: p.corner_ratio for k, p in planes.items()},
            "corner_colour_cast_pct": res.corner_colour_cast_pct,
            "lens": self.lens,
        }
        rel = ", ".join(
            f"{k}/G {p.relative_to_green:.2f}" for k, p in planes.items() if not k.startswith("G")
        )
        prnu = " ".join(f"{k} {p.prnu_pct:.2f}" for k, p in planes.items())
        msg = f"{rel}; PRNU % {prnu}"
        if res.green_imbalance_pct is not None:
            msg += f"; green imbalance {res.green_imbalance_pct:+.2f} %"
        return self._record(FpResult(step="colour", status="pass", message=msg, data=data))
