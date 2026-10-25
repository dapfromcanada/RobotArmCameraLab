"""State and camera helpers shared by the fingerprint measurement modules."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np
import numpy.typing as npt

from camera_lab.two_d.errors import InvalidRequestError
from camera_lab.two_d.fingerprint import measure as fm
from camera_lab.two_d.fingerprint.bench import Bench, BenchLens
from camera_lab.two_d.models import PIXEL_FORMAT_BITS, CameraSettingsPatch

if TYPE_CHECKING:
    from camera_lab.two_d.profiles import FingerprintRecipe, FpLight
    from camera_lab.two_d.service import Actor, TwoDService
from camera_lab.two_d.fingerprint.common import (
    FLICKER_MAX_INFLATION,
    FP_SETUPS,
    MAX_EXPOSURE_US,
    PROBE_FRACTION,
    FpResult,
    FpSetup,
    FpStep,
    StepPlan,
)


class SessionBase:
    """What every measurement module sees: the session state and the camera helpers."""

    service: TwoDService
    camera_id: str
    actor: Actor
    root: Path | None
    results: dict[FpStep, FpResult]  # per measurement
    setup_results: dict[FpSetup, FpResult]  # per setup (the rows)
    durations: dict[str, dict[str, float]]  # seconds per setup → measurement
    temperatures: dict[str, float]  # camera °C at the start of each setup
    gain_adjustable: bool  # the camera has a gain control (measured even if the mode pins it)
    mode_gain_db: float  # the gain the design mode leaves (restored after gain sweeps)
    deferred_gains: dict[str, list[float]]  # gain mode → steps left to the low-light row
    plan: dict[FpSetup, StepPlan]
    backup_id: str | None
    fmt: str
    full_scale: float
    black_dn: float
    mode: str
    gain_knob: bool  # gain is a knob of the design mode
    hdr_capable: bool
    colour: str  # the profile's sensor colour: bayer, prism, mono or unknown
    hdr_channels: tuple[str, ...]  # hdr.output options of single channels
    channel_fmt: str
    lens: str  # lens / iris (from the recipe, or as the human states it)
    lens_label: str
    lens_id: str | None  # the picked lens (catalog id)
    lens_spec: BenchLens | None  # its marked stops and measured light per stop
    lens_choices: list[tuple[str, BenchLens, bool]]  # (id, lens, tested on this model)
    iris_f: float | None  # discovery rows: the stop the human picked
    camera_model: str
    catalog_path: Path | None
    recipe: FingerprintRecipe | None
    bench: Bench | None
    created: datetime
    _view_exposure: float
    _view_gain: float
    _ref_exposure: float  # exposure of the flat sweep's reference point
    _ref_rate: float  # own-output signal per µs at the reference light (flat step)
    _last_fraction: float  # signal / span at the end of the last exposure search
    _light_changes: bool
    _open_f: float
    _channel_black: dict[str, fm.DarkPoint]
    _channels: dict[str, dict[str, Any]]  # measured channels, kept across runs
    _arrays: dict[str, npt.NDArray[Any]]
    min_exposure_us: float  # lit measurements (bench light ripple)
    reset_on_start: bool  # identity starts from factory Default (never trust the boot state)
    _row_noise_dn2: dict[str, float]  # sensor row noise per output ("HDR"/own, HDRChannelN)

    def _temperature(self) -> float | None:
        """The camera's internal temperature (°C), where it tells."""
        try:
            return self.service._open[self.camera_id].camera.temperature_c()
        except Exception:  # read-only nicety: never stops a measurement
            return None

    def _record(self, res: FpResult) -> FpResult:
        self.results[res.step] = res  # type: ignore[index]
        return res

    def _record_setup(self, res: FpResult) -> FpResult:
        self.setup_results[res.step] = res  # type: ignore[index]
        return res

    def _require(self, *steps: FpStep) -> None:
        missing = [s for s in steps if s not in self.results or not self.results[s].ok]
        if missing:
            raise InvalidRequestError(f"run these fingerprint steps first: {', '.join(missing)}")

    def _plane(self) -> npt.NDArray[np.float64]:
        f = self.service.grab(self.camera_id, int(MAX_EXPOSURE_US / 1000) + 5000)
        return fm.raw_plane(f.data, f.pixel_format)

    def _frame(self) -> npt.NDArray[Any]:
        """One full raw frame (all colour sites / channels)."""
        f = self.service.grab(self.camera_id, int(MAX_EXPOSURE_US / 1000) + 5000)
        return np.asarray(f.data)

    def _lit_range(self) -> tuple[float, float]:
        """Exposure range for lit measurements: the camera's, from the bench minimum (the
        light's ripple averages out above it) up to MAX_EXPOSURE_US."""
        lo, hi = self._range("exposure_us")
        return max(lo, self.min_exposure_us), min(hi, MAX_EXPOSURE_US)

    def _pair_point(self, exposure_us: float) -> fm.PtcPoint:
        """Mean, temporal variance and its row-correlated part of a frame pair."""
        a, b = fm.centre(self._plane()), fm.centre(self._plane())
        mean, var = fm.pair_temporal(a, b)
        return fm.PtcPoint(
            exposure_us=exposure_us, mean_dn=mean, var_dn2=var, row_var_dn2=fm.row_variance(a, b)
        )

    def _flicker_problem(self, points: list[fm.PtcPoint], output: str) -> tuple[float, str | None]:
        """(worst inflation, a STOP message when row banding adds more than
        FLICKER_MAX_INFLATION to the noise)."""
        worst, at = fm.flicker_inflation(points, self._row_noise_dn2.get(output, 0.0))
        if worst <= FLICKER_MAX_INFLATION:
            return worst, None
        return worst, (
            f"the light flickers: row banding adds {worst * 100:.0f} % to the noise at "
            f"{at / 1000:.2f} ms exposure (rolling shutter). Use a steadier light, or less "
            "light (smaller iris) so the exposures can be longer"
        )

    def _set(self, **kw: Any) -> None:
        self.service.apply(self.camera_id, CameraSettingsPatch(**kw), self.actor, mode="clamp")

    def _range(self, name: str) -> tuple[float, float]:
        rng = self.service.get_settings(self.camera_id).ranges[name]
        return float(rng.min.value), float(rng.max.value)  # type: ignore[union-attr]

    def _settings(self) -> Any:
        return self.service.get_settings(self.camera_id).settings

    def _gain(self) -> float:
        """Gain for the measurements: the lowest where gain is a knob, else as the mode left it."""
        return self._range("gain_db")[0] if self.gain_knob else float(self._settings().gain_db)

    def _output(self, output: str, fmt: str) -> float:
        """Select an HDR output and pixel format; return its full scale (the service stops
        the stream for these)."""
        self._set(pixel_format=fmt, advanced={"hdr.output": output})
        return float((1 << PIXEL_FORMAT_BITS[fmt]) - 1)

    def _exposure_for(
        self,
        fraction: float,
        e_lo: float,
        e_hi: float,
        *,
        full_scale: float | None = None,
        black: float | None = None,
    ) -> float | None:
        """Exposure giving ``fraction`` of the range above black (None if out of reach)."""
        full = self.full_scale if full_scale is None else full_scale
        blk = self.black_dn if black is None else black
        span = full - blk
        exp = float(self._settings().exposure_us)
        for _ in range(8):
            m = float(np.mean(fm.centre(self._plane()))) - blk
            if m >= 0.98 * span:  # clipped: halve
                exp = max(e_lo, exp / 2)
            elif abs(m - fraction * span) <= 0.03 * span:
                return exp
            else:
                exp = min(e_hi, max(e_lo, exp * fraction * span / max(m, 1.0)))
            self._set(exposure_us=exp)
        m = float(np.mean(fm.centre(self._plane()))) - blk
        self._last_fraction = m / span
        return exp if abs(m - fraction * span) <= 0.1 * span else None

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
        return max(m - self._dark_signal(exp), 0.0) / exp

    def _dark_signal(self, exposure_us: float) -> float:
        """What the dark current adds above the black level at ``exposure_us`` (the closed-iris
        rows run long exposures: 2026-10-25 sim, +14 % light without this)."""
        dark = self.results.get("dark")
        rate = dark.data.get("dark_current_dn_s") if dark is not None else None
        return float(rate) * exposure_us / 1e6 if isinstance(rate, int | float) else 0.0

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

    def _lens_at(self, light: FpLight) -> str:
        return f"{self.lens_label}, f/{light.f_number:g}"

    def _iris_text(self, f_number: float) -> str:
        closed = max(self.lens_spec.f_stops) if self.lens_spec is not None else None
        end = (
            " (fully open)"
            if f_number == self._open_f
            else " (fully closed)"
            if f_number == closed
            else ""
        )
        return f"Iris at f/{f_number:g}{end}"

    def _reference(self) -> FpLight:
        """The reference light: the recipe's, else the bench light at its brightest setting with
        the iris fully open (an end stop: the human can always find it again)."""
        from camera_lab.two_d.profiles import FpLight

        if self.recipe is not None:
            return self.recipe.reference
        brightest = max(self.bench.light.relative) if self.bench is not None else 10
        return FpLight(dimmer=brightest, f_number=self._open_f or 1.0)

    def _expected(self, light: FpLight) -> float:
        """Light at ``light`` relative to the reference."""
        if self.bench is None:  # pragma: no cover - recipe mode needs the bench file
            return 1.0
        ref = self._reference()
        return self.bench.light_ratio(
            light.dimmer, light.f_number, ref.dimmer, ref.f_number, self.lens_spec
        )

    def _planned(self) -> list[FpStep]:
        """Measurements of the active setups, in order."""
        return [m for s in FP_SETUPS if self.plan[s].active for m in self.plan[s].measures]
