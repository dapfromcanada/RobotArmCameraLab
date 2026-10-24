"""Verify a camera's state on whatever it sees — the white paper's checks, as a command.

1. ``mode``       — every setting of the design mode is in place (read back).
2. ``gain``       — the gain the camera model's baseline was measured at (a digital gain
                    would scale every level and clip the highlights).
3. ``camera_state`` — linear output: exposure E/2 and E give × 2.00 (× 1.41 = gamma LUT).
4. ``dark_clip``  — no pixel reads exactly 0 (black level high enough: noise not clipped).
5. ``shadow_step`` — the finest step between shadow values is 1 DN (no digital gain).

Nothing is changed except the exposure, briefly (check 3, and checks 4–5 at the shortest
exposure, where many pixels are near black); it is put back.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

from camera_lab.two_d.camera_models import load_model_sheet
from camera_lab.two_d.site.light import SiteCheck, _Assessment

if TYPE_CHECKING:
    from camera_lab.two_d.service import Actor, TwoDService

ZERO_PIXELS_MAX_PCT = 0.1
SHADOW_PCT = 5.0  # the darkest 5 % of the pixels are the "shadows"
SHADOW_MIN_VALUES = 20  # distinct shadow values needed to judge the step
STEP_MAX_DN = 1.5


def verify_camera(
    service: TwoDService,
    camera_id: str,
    *,
    actor: Actor = "human",
    sheet_root: Path | None = None,
) -> list[SiteCheck]:
    """Run the state checks; returns them (pass / warn / fail / info with actions)."""
    serial = service.resolve(camera_id)
    if not service.is_open(serial):
        service.open(serial, "verify")
    info = service._open[serial].camera.info
    sheet = load_model_sheet(info.model, sheet_root)
    prof = service.get_profile(serial)
    design = sheet.design_mode if sheet and sheet.design_mode in prof.modes else prof.default_mode
    a = _Assessment(service, serial, sheet, actor, None)
    mism = service.mode_mismatches(serial, design)
    a.add(
        SiteCheck(
            name="mode",
            status="pass" if not mism else "fail",
            message=f"design mode {design}: "
            + ("every setting in place" if not mism else "; ".join(mism)),
            action="" if not mism else f"run `two-d reset {serial}` (or MCP reset_camera)",
            data={"design_mode": design, "mismatches": mism},
        )
    )
    a.gain_condition()
    start = float(service.get_settings(serial).settings.exposure_us)
    try:
        a.set_budget()
        a.camera_state()
        # the dark checks at the shortest exposure: plenty of pixels near black even on a
        # bright scene (third fresh-agent test 2026-10-24: inconclusive at the working one)
        a.set_exposure(a.exposure_range()[0])
        img = a.plane()
    finally:
        a.set_exposure(start)
    zero = float(np.mean(img == 0) * 100)
    a.add(
        SiteCheck(
            name="dark_clip",
            status="pass" if zero <= ZERO_PIXELS_MAX_PCT else "fail",
            message=f"{zero:.3f} % of pixels read exactly 0",
            action=""
            if zero <= ZERO_PIXELS_MAX_PCT
            else "dark noise is clipped at 0: raise the black level (black.level ≥ 1)",
            data={"zero_pct": zero},
        )
    )
    # the finest step only exists in the first quantization regime (the most sensitive HDR
    # channel); above it the step is one code of a less sensitive channel (20, 153, ... DN)
    q = sorted(sheet.performance.quantization, key=lambda r: r.from_level) if sheet else []
    first = q[0].step_dn if q else 1.0
    if len(q) > 1:
        cut = a.black + q[1].from_level * (a.full - a.black)
    else:
        cut = float(np.percentile(img, SHADOW_PCT))
    shadows = np.unique(img[img <= cut].astype(np.int64))
    if shadows.size < SHADOW_MIN_VALUES:
        a.add(
            SiteCheck(
                name="shadow_step",
                status="info",
                message=f"only {shadows.size} distinct values below {cut:.0f} DN (the finest "
                "quantization regime): too few to judge the step — the scene has no pixels that "
                "dark at this exposure",
                data={"distinct": int(shadows.size)},
            )
        )
    else:
        step = float(np.median(np.diff(shadows)))
        gain_db = 20 * math.log10(step / first) if step > 0 else 0.0
        ok = step <= first * STEP_MAX_DN
        a.add(
            SiteCheck(
                name="shadow_step",
                status="pass" if ok else "warn",
                message=f"finest step between shadow values: {step:g} DN"
                + ("" if ok else f" (≈ {gain_db:.1f} dB of digital gain)"),
                action="" if ok else "set gain_db 0 (gain after the merge only scales and clips)",
                data={"step_dn": step, "distinct": int(shadows.size)},
            )
        )
    return a.checks
