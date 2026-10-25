"""Fingerprint types, constants and small helpers shared by the measurement modules."""

from __future__ import annotations

import math
import re
import statistics
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from camera_lab.core.paths import ws_root

if TYPE_CHECKING:
    pass


FpStep = Literal[
    "identity",
    "timing",
    "dark",
    "flat",
    "colour",
    "channels_a",
    "channels_b",
    "channels_c",
    "channels_d",
    "light_back",
    "noise_gain",
    "lens_iris",
    "user_sets",
    "report",
]
STAGE_STEPS: tuple[FpStep, ...] = ("channels_a", "channels_b", "channels_c", "channels_d")
#: What the human sets up (Derek 2026-10-22: one test per bench state): each setup is one
#: row with one checklist and one Run, and makes several measurements (FpStep).
FpSetup = Literal["cap", "diffuser", "light_2", "light_3", "light_4", "lens", "finish"]
LIGHT_SETUPS: tuple[FpSetup, ...] = ("light_2", "light_3", "light_4")
FP_SETUPS: tuple[FpSetup, ...] = ("cap", "diffuser", *LIGHT_SETUPS, "lens", "finish")
MEASURE_LABELS: dict[FpStep, str] = {
    "identity": "identity",
    "timing": "timing",
    "dark": "darks",
    "flat": "flat",
    "colour": "colour planes",
    "channels_a": "HDR",
    "channels_b": "HDR",
    "channels_c": "HDR",
    "channels_d": "HDR",
    "light_back": "light back",
    "noise_gain": "noise vs gain",
    "lens_iris": "lens / iris",
    "user_sets": "user sets",
    "report": "report",
}
#: Each repeated measurement runs this many times in one Run (Derek 2026-10-22: 3).
REPEATS = 3
REPEAT_TOLERANCE_PCT = 10.0  # spread (max − min) / median above this → "not repeatable"
DARK_REPEAT_KEYS = ("black_dn", "read_noise_dn")
FLAT_REPEAT_KEYS = (
    "reference_light_dn_per_us",
    "flat.prnu_pct",
    "flat.vignetting_corner_ratio",
    "photon_transfer.system_gain_dn_per_e",
    "photon_transfer.full_well_e",
)
COLOUR_FRAMES = 6
PIPELINE_FRAMES = 8  # frames timed for the receive interval
#: The user-set persistence test writes its marker values here (a scratch slot).
PERSIST_SLOT = "UserSet2"
#: Cameras without a boot-set feature boot from this slot once anything was saved (JAI,
#: power-cycle test 2026-10-21): the test uses it and then refills it with the factory
#: Default settings, so the camera boots as from Default.
BOOT_SLOT = "UserSet1"
#: The lens row closes the iris this many f-stops from the diffuser row.
LENS_STOPS_CLOSED = 2
#: Lit measurements use exposures of at least this (bench file `min_exposure_us` overrides):
#: bench LEDs ripple (~4 kHz on the "DC" floods, 120 Hz on the dimmed panels) and a short
#: rolling-shutter exposure turns that into row bands (measured 2026-10-23).
DEFAULT_MIN_EXPOSURE_US = 2000.0
#: Row banding may add at most this share to a point's temporal variance.
FLICKER_MAX_INFLATION = 0.02
COLOUR_REPEAT_KEYS = (
    "relative_to_green.R",
    "relative_to_green.B",
    "prnu_pct.R",
    "prnu_pct.G",
    "prnu_pct.Gr",
    "prnu_pct.Gb",
    "prnu_pct.B",
)
#: Not the photon-transfer read noise: it is the intercept of a line dominated by shot
#: noise and swings by > 100 % between runs; the darks measure read noise directly.
CHANNEL_REPEAT_KEYS = ("system_gain_dn_per_e", "full_well_e")
FP_STEPS: tuple[FpStep, ...] = (
    "identity",
    "timing",
    "dark",
    "flat",
    "colour",
    *STAGE_STEPS,
    "light_back",
    "noise_gain",
    "lens_iris",
    "user_sets",
    "report",
)

DARK_FRAMES = 4
DARK_EXPOSURES_US = (1_000.0, 10_000.0, 100_000.0)
DARK_GAIN_STEP_DB = 6.0
FLAT_TARGET = 0.80  # of (full scale − black): the sweep's reference point
FLAT_SWEEP = (
    0.02,
    0.04,
    0.07,
    0.1,
    0.15,
    0.2,
    0.3,
    0.4,
    0.5,
    0.6,
    0.7,
    0.8,
    0.9,
    1.0,
    1.15,
    1.3,
    1.5,
)
FLAT_MASTER_LEVEL = 0.5
FLAT_MASTER_FRAMES = 8
NOISE_TARGET = 0.25
NOISE_GAIN_STEP_DB = 3.0
NOISE_PAIRS = 3
GAIN_SWITCH_GAIN = 1.05  # SNR up by > 5 % for more gain → a gain-mode switch
MAX_EXPOSURE_US = 2_000_000.0
SETTLE_FRAMES = 3
SETTLE_MAX_LEVEL = 0.9  # of full scale: above, halve the exposure before the settle test
SETTLE_MAX_HALVINGS = 6
#: Black level for the fingerprint where the camera offers ``black.level`` and sits below
#: it (TRI054S-C: 0 by default, so dark noise is clipped at 0). In the camera's unit
#: (Lucid: % of full scale, live range 0–12.5); restored with the backup at the end.
#: Live TRI054S-C 2026-10-22: 1 % → +30–40 DN, 5 % → +190–205 DN on a 12-bit channel
#: output, whose noise near black is ~45 DN; the combined 24-bit output ignores it.
BLACK_RAISE = 5.0
CHANNEL_PREFIX = "HDRChannel"  # hdr.output options that select one HDR channel
#: A full channel sweep needs its 80 % point at ≥ this × the shortest exposure (the sweep
#: goes down to 2 % of the 80 % exposure; at 1/10 three fit points are still unclamped).
SWEEP_LOW_FACTOR = 10.0
LENS_MAX_CHARS = 200
#: The measured light may differ from the recipe's expectation by this factor (iris marks
#: and the dimmer are not exact; one f-stop = 2×, one dimmer step at the low end ≈ 1.5×).
LIGHT_TOLERANCE = 1.35
#: Back at the reference (iris on its end stop, same dimmer): much tighter.
LIGHT_BACK_TOLERANCE = 1.12
PROBE_FRACTION = 0.3  # light probe: aim at 30 % of the camera's own output
F_STOPS = (1.0, 1.4, 2.0, 2.8, 4.0, 5.6, 8.0, 11.0, 16.0, 22.0, 32.0)
_F_NUMBER_RE = re.compile(r"f\s*/\s*(\d+(?:\.\d+)?)", re.IGNORECASE)


def iris_hint(factor: float, lens: str) -> str:
    """The iris equivalent of changing the light by ``factor`` (> 1: less light).

    One f-stop halves the light; with an f-number in ``lens`` (e.g. "f/1.4") the nearest
    marked stop to turn to is named.
    """
    if factor <= 0:
        return ""
    stops = math.log2(factor)
    word = "close" if stops > 0 else "open"
    text = f" ≈ {abs(stops):.1f} f-stops: {word} the iris"
    m = _F_NUMBER_RE.search(lens)
    if m:
        now = float(m.group(1))
        target = now * math.sqrt(factor)
        nearest = min(F_STOPS, key=lambda f: abs(math.log(f / target)))
        text += f" (f/{now:g} → about f/{nearest:g})"
    return text


def _dig(data: Any, path: str) -> Any:
    for key in path.split("."):
        if not isinstance(data, dict) or key not in data:
            return None
        data = data[key]
    return data


def repeat_summary(runs: list[dict[str, Any]], keys: Iterable[str]) -> dict[str, Any]:
    """Median and spread ((max − min) / median, %) of each key over repeated runs."""
    out: dict[str, Any] = {}
    for key in keys:
        vals = [float(v) for d in runs if isinstance(v := _dig(d, key), int | float)]
        vals = [v for v in vals if math.isfinite(v)]
        if len(vals) < 2:
            continue
        med = statistics.median(vals)
        spread = (max(vals) - min(vals)) / abs(med) * 100 if med else 0.0
        out[key] = {"median": med, "spread_pct": round(spread, 2), "values": vals}
    return out


def repeat_note(summary: dict[str, Any]) -> str:
    if not summary:
        return ""
    bad = {k: v for k, v in summary.items() if v["spread_pct"] > REPEAT_TOLERANCE_PCT}
    if bad:
        return " — NOT repeatable: " + ", ".join(
            f"{k.rsplit('.', 1)[-1]} varies {v['spread_pct']:.0f} %" for k, v in bad.items()
        )
    worst = max(v["spread_pct"] for v in summary.values())
    return f" — {REPEATS} runs agree within {worst:.1f} %"


@dataclass(frozen=True)
class StepPlan:
    """What a setup asks of the human (label, instruction, checklist) and what it measures."""

    step: FpSetup
    label: str
    instruction: str
    checks: tuple[str, ...] = ()
    measures: tuple[FpStep, ...] = ()
    active: bool = True
    can_skip: bool = False
    #: The iris the row runs at (camera_lab fills in the lens / iris text from it).
    f_number: float | None = None
    #: The human picks the stop (HDR discovery rows: the light each channel asks for).
    iris_pick: bool = False


class FpResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    step: str  # a measurement (FpStep) or a setup (FpSetup)
    status: Literal["pass", "fail", "skipped"]
    message: str
    data: dict[str, Any] = Field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status != "fail"


def format_family(pixel_format: str) -> str:
    """Raw family of a pixel format: the Bayer pattern, ``RGB`` or ``Mono``.

    The fingerprint stays in the camera's own family: on a colour sensor "Mono12"
    is a converted luminance, not raw data.
    """
    if pixel_format.startswith("Bayer"):
        return pixel_format[:7]
    for fam in ("RGB", "BGR", "Mono"):
        if pixel_format.startswith(fam):
            return fam
    return pixel_format


def fingerprints_root() -> Path:
    """``WS/config/camera_fingerprints``."""
    return ws_root() / "config" / "camera_fingerprints"
