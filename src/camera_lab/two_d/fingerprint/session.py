"""Camera fingerprint procedure (Phase 11): gated, measured once per camera in the lab.

Steps (each passes or stops and says what the human must do):

1. ``identity``   — backup; the camera's design mode (the profile's default mode) and
                    its raw format (the mode's own if ≥ 12 bit, else the best ≤ 12-bit
                    raw format of the sensor's family); black level raised a little
                    so the darks are not clipped at 0; gain 0 if gain is a knob
2. ``timing``     — fps per pixel format; stale frames after an exposure change
                    (camera_lab's settle logic should leave 0)
3. ``dark``       — lens cap on: black level, read noise, DSNU (per gain where gain
                    is a knob), dark current, hot pixels; a master dark is saved.
                    HDR cameras: also the black and read noise of each channel
4. ``flat``       — opal diffuser close to the lens: exposure sweep → linearity,
                    saturation, SNR across the range, photon transfer (system gain,
                    read noise, full well); vignetting and PRNU; a master flat.
                    HDR cameras: the combined output gets the response only (EMVA
                    1288 photon transfer applies per channel, not to merged data)
5. ``channels_a`` … ``channels_d`` — HDR cameras: photon transfer of each channel on
                    its own 12-bit output, at the light the profile's recipe dictates
                    (dimmer + f-number per stage). The light is measured first and the
                    stage stops if it does not match. Without a recipe (a new model):
                    one discovery stage that says how much less/more light each channel
                    needs. Unused stages are skipped
6. ``light_back`` — the bench back at the reference light (iris fully open), verified
                    against the light measured in the flat step
7. ``noise_gain`` — every camera with a gain control (also where the mode pins it):
                    every 3 dB step in each gain mode at a constant output, photon
                    transfer per gain, gain-mode switches, where each mode clips; steps
                    that need less than the minimum exposure go to the low-light row
   ``lens_iris_N`` — one row per marked stop between the lens's end stops: the light
                    the stop passes (measured, the check tolerance halfway to the next
                    stop), vignetting and per-colour shading
   ``low_light``  — iris fully closed: signals from ~1 read noise up, gain at a fixed
                    exposure, the deferred gain steps, N-frame averaging (√N)
8. ``user_sets``  — what survives a restart: markers → a boot user set → reboot →
                    kept / lost. Boot-set cameras use User Set 2; cameras without (JAI)
                    User Set 1, refilled with factory Default afterwards
9. ``report``     — ``fingerprint.yaml`` + ``report.md`` in a dated folder under
                    ``WS/config/camera_fingerprints/<serial>/``, then the run into the
                    catalog (SQLite); the camera is restored from the identity backup

Darks: every exposure from 1 ms to 2 s, every gain step in each gain mode. Every row
records the camera temperature and the lens / iris camera_lab filled in (picked, never
typed).

Strict procedure (Derek 2026-10-22): every step has a checklist of what the human must
have done; :meth:`FingerprintSession.run` refuses a step until each item is confirmed.

Bench vs machine (spec): the camera-only results carry over to the machine;
vignetting / flat field depend on the lens, aperture and focus and are re-measured
there.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from camera_lab.two_d.errors import InvalidRequestError
from camera_lab.two_d.fingerprint.bench import lens_choices, load_bench

if TYPE_CHECKING:
    from camera_lab.two_d.service import Actor, TwoDService
from camera_lab.two_d.fingerprint.colour import ColourMeasurements
from camera_lab.two_d.fingerprint.common import (
    BLACK_RAISE,
    CHANNEL_PREFIX,
    CHANNEL_REPEAT_KEYS,
    COLOUR_REPEAT_KEYS,
    DARK_EXPOSURES_US,
    DARK_FRAMES,
    DARK_GAIN_STEP_DB,
    DARK_REPEAT_KEYS,
    DEFAULT_MIN_EXPOSURE_US,
    F_STOPS,
    FLAT_MASTER_FRAMES,
    FLAT_MASTER_LEVEL,
    FLAT_REPEAT_KEYS,
    FLAT_SWEEP,
    FLAT_TARGET,
    FP_SETUPS,
    FP_STEPS,
    GAIN_SWITCH_GAIN,
    LENS_MAX_CHARS,
    LENS_STEPS,
    LIGHT_BACK_TOLERANCE,
    LIGHT_SETUPS,
    LIGHT_TOLERANCE,
    MAX_EXPOSURE_US,
    MEASURE_LABELS,
    NOISE_GAIN_STEP_DB,
    NOISE_PAIRS,
    NOISE_TARGET,
    PROBE_FRACTION,
    REPEAT_TOLERANCE_PCT,
    REPEATS,
    SETTLE_FRAMES,
    STAGE_STEPS,
    SWEEP_LOW_FACTOR,
    FpResult,
    FpSetup,
    FpStep,
    StepPlan,
    fingerprints_root,
    format_family,
    iris_hint,
    repeat_note,
    repeat_summary,
)
from camera_lab.two_d.fingerprint.core import CoreMeasurements
from camera_lab.two_d.fingerprint.hdr import HdrMeasurements
from camera_lab.two_d.fingerprint.lens import LensMeasurements
from camera_lab.two_d.fingerprint.lowlight import LowLightMeasurements
from camera_lab.two_d.fingerprint.plan import PlanMixin
from camera_lab.two_d.fingerprint.report import ReportMixin

__all__ = [
    "BLACK_RAISE",
    "CHANNEL_PREFIX",
    "CHANNEL_REPEAT_KEYS",
    "DARK_EXPOSURES_US",
    "DARK_FRAMES",
    "DARK_GAIN_STEP_DB",
    "DARK_REPEAT_KEYS",
    "FLAT_MASTER_FRAMES",
    "FLAT_MASTER_LEVEL",
    "FLAT_REPEAT_KEYS",
    "FLAT_SWEEP",
    "FLAT_TARGET",
    "FP_SETUPS",
    "FP_STEPS",
    "F_STOPS",
    "GAIN_SWITCH_GAIN",
    "LENS_MAX_CHARS",
    "LIGHT_BACK_TOLERANCE",
    "LIGHT_SETUPS",
    "LIGHT_TOLERANCE",
    "MAX_EXPOSURE_US",
    "MEASURE_LABELS",
    "NOISE_GAIN_STEP_DB",
    "NOISE_PAIRS",
    "NOISE_TARGET",
    "PROBE_FRACTION",
    "REPEATS",
    "REPEAT_TOLERANCE_PCT",
    "SETTLE_FRAMES",
    "STAGE_STEPS",
    "SWEEP_LOW_FACTOR",
    "FingerprintSession",
    "FpResult",
    "FpSetup",
    "FpStep",
    "StepPlan",
    "fingerprints_root",
    "format_family",
    "iris_hint",
    "repeat_note",
    "repeat_summary",
]


class FingerprintSession(
    PlanMixin,
    LowLightMeasurements,
    CoreMeasurements,
    ColourMeasurements,
    HdrMeasurements,
    LensMeasurements,
    ReportMixin,
):
    """One fingerprint of one open camera."""

    def __init__(
        self,
        service: TwoDService,
        camera_id: str,
        *,
        actor: Actor = "human",
        root: Path | None = None,
        bench_path: Path | None = None,
        reset: bool = True,
        catalog_path: Path | None = None,
    ) -> None:
        self.reset_on_start = reset  # factory Default, then the design mode
        self.service = service
        self.camera_id = service.resolve(camera_id)
        self.actor = actor
        self.root = root
        self.results = {}  # per measurement
        self.setup_results = {}
        self.durations = {}
        self.temperatures = {}  # camera °C at the start of each row
        self.gain_adjustable = False
        self.deferred_gains = {}  # gain steps the bright row left to the low-light row
        self.mode_gain_db = 0.0
        self._light_changes = False
        self.backup_id = None
        self.fmt = ""
        self.full_scale = 0.0
        self.black_dn = 0.0
        self._view_exposure = 0.0
        self._view_gain = 0.0
        self._ref_exposure = 0.0  # exposure of the flat sweep's reference point
        self.mode = ""
        self.gain_knob = True  # gain is a knob of the design mode
        self.hdr_channels = ()  # hdr.output options of single channels
        self.channel_fmt = ""
        self._channel_black = {}
        self._channels = {}  # measured channels, kept across runs
        self.lens = ""  # lens / iris text of the row (camera_lab fills it in, never typed)
        self._last_fraction = 0.0  # signal / span at the end of the last exposure search
        self._ref_rate = 0.0  # own-output signal per µs at the reference light (flat step)
        self._arrays = {}
        self.created = datetime.now(UTC)
        prof = service.get_profile(self.camera_id)
        self.hdr_capable = prof.characteristics.hdr_capable
        self.colour = prof.characteristics.colour
        self.recipe = prof.fingerprint
        self.bench = load_bench(bench_path)
        self.min_exposure_us = (
            self.bench.min_exposure_us if self.bench is not None else DEFAULT_MIN_EXPOSURE_US
        )
        glob = self.bench.min_exposure_global_us if self.bench is not None else None
        if prof.characteristics.shutter == "global" and glob is not None:
            self.min_exposure_us = glob
        self._row_noise_dn2 = {}
        self.lens_label = ""
        self._open_f = 0.0
        self.lens_id = None
        self.lens_spec = None
        self.iris_f = None  # discovery rows: the stop the human picked
        self.camera_model = service._open[self.camera_id].camera.info.model
        self.catalog_path = catalog_path
        self.lens_choices = lens_choices(self.camera_model, self.bench, catalog_path)
        default = self.recipe.lens if self.recipe is not None else None
        if default is None:
            tested = [lid for lid, _, tried in self.lens_choices if tried]
            only = [lid for lid, _, _ in self.lens_choices]
            default = tested[0] if len(tested) == 1 else only[0] if len(only) == 1 else None
        if default is not None:
            self.select_lens(default)
        else:
            self.plan = self._build_plan()

    def select_lens(self, lens_id: str) -> None:
        """The lens being tested, picked from :attr:`lens_choices` (never typed). Fixed once a
        row has run; with a recipe the recipe's stops must be marked on the picked lens."""
        if any(r.status == "pass" for r in self.setup_results.values()):
            raise InvalidRequestError(
                "the lens is fixed once a row has run: start a new fingerprint"
            )
        specs = {lid: spec for lid, spec, _ in self.lens_choices}
        spec = specs.get(lens_id)
        if spec is None:
            raise InvalidRequestError(
                f"lens {lens_id!r} is not in the catalog (known: {', '.join(specs) or 'none'}): "
                "add it first"
            )
        r = self.recipe
        if r is not None:
            if self.bench is None:
                raise InvalidRequestError(
                    "the camera profile has a fingerprint recipe but there is no "
                    "config/fingerprint_bench.yaml"
                )
            for light in (r.reference, *r.channel_stages):
                if light.f_number not in spec.f_stops:
                    raise InvalidRequestError(
                        f"recipe f/{light.f_number:g} is not a marked stop of {spec.label} "
                        f"({', '.join(f'f/{f:g}' for f in spec.f_stops)})"
                    )
                self.bench.light_ratio(light.dimmer, light.f_number, r.reference.dimmer, 1.0)
        self.lens_id, self.lens_spec = lens_id, spec
        self.lens_label, self._open_f = spec.label, spec.open_f
        self.lens = self._lens_at(self._reference())
        self.iris_f = self._reference().f_number  # discovery rows start where the diffuser was
        self.plan = self._build_plan()

    def reload_lenses(self) -> None:
        """Read the lens list again (after a lens was added to the catalog)."""
        self.lens_choices = lens_choices(self.camera_model, self.bench, self.catalog_path)

    def set_iris(self, f_number: float) -> None:
        """Discovery rows only (an HDR camera without a recipe): the marked stop the human set,
        picked from the lens's stops."""
        if self.lens_spec is None or f_number not in self.lens_spec.f_stops:
            raise InvalidRequestError(
                "pick a marked stop of the lens"
                + (
                    f" ({', '.join(f'f/{f:g}' for f in self.lens_spec.f_stops)})"
                    if self.lens_spec
                    else ""
                )
            )
        self.iris_f = f_number
        self.lens = f"{self.lens_label}, f/{f_number:g}"

    def _measure(self, m: FpStep) -> FpResult:
        """One measurement; darks and the flat run REPEATS times (HDR channels repeat inside)."""
        if m == "dark":
            res = self._repeat(self.step_dark, DARK_REPEAT_KEYS)
            if res.ok and "repeats" in res.data:
                self.black_dn = res.data["repeats"]["black_dn"]["median"]
            return res
        if m == "flat":
            res = self._repeat(self.step_flat, FLAT_REPEAT_KEYS)
            rate = res.data.get("repeats", {}).get("reference_light_dn_per_us")
            if res.ok and rate:
                self._ref_rate = rate["median"]
            return res
        if m == "colour":
            return self._repeat(self.step_colour, COLOUR_REPEAT_KEYS)
        if m in STAGE_STEPS:
            return self.step_stage(STAGE_STEPS.index(m))
        if m in LENS_STEPS:
            return self.step_lens_iris(LENS_STEPS.index(m))
        methods: dict[FpStep, Callable[[], FpResult]] = {
            "identity": self.step_identity,
            "timing": self.step_timing,
            "light_back": self.step_light_back,
            "noise_gain": self.step_noise_gain,
            "low_light": self.step_low_light,
            "user_sets": self.step_user_sets,
            "report": self.step_report,
        }
        return methods[m]()

    def _repeat(self, fn: Callable[[], FpResult], keys: tuple[str, ...]) -> FpResult:
        runs: list[FpResult] = []
        for _ in range(REPEATS):
            res = fn()
            if not res.ok:
                return res
            runs.append(res)
        summary = repeat_summary([r.data for r in runs], keys)
        last = runs[-1]
        return self._record(
            FpResult(
                step=last.step,
                status=last.status,
                message=last.message + repeat_note(summary),
                data={**last.data, "repeats": summary},
            )
        )

    def run(self, setup: FpSetup, confirmed: Iterable[str] = ()) -> FpResult:
        """Run every measurement of ``setup`` once its checklist is confirmed. On a Retry the
        measurements that already passed in this setup are kept."""
        plan = self.plan[setup]
        done = set(confirmed)
        missing = [c for c in plan.checks if c not in done]
        if missing:
            raise InvalidRequestError(
                f"confirm the checklist of {plan.label} first: {'; '.join(missing)}"
            )
        if not plan.active:
            return self._record_setup(
                FpResult(step=setup, status="skipped", message=plan.instruction)
            )
        if self.lens_spec is None:
            raise InvalidRequestError(
                "pick the lens being tested first (or add it: Add a lens); it is recorded with "
                "every row"
            )
        if plan.iris_pick:
            if self.iris_f is None:
                raise InvalidRequestError("pick the stop the iris is set to first")
            self.lens = f"{self.lens_label}, f/{self.iris_f:g}"
        elif plan.f_number is not None:
            self.lens = f"{self.lens_label}, f/{plan.f_number:g}"
        temp = self._temperature()
        if temp is not None:
            self.temperatures[setup] = temp
        parts: list[FpResult] = []
        durations = self.durations.setdefault(setup, {})  # filled as it goes: the report
        for m in plan.measures:  # (last in the finish row) sees the row's earlier times
            prev = self.results.get(m)
            if prev is not None and prev.ok:  # kept from an earlier try: keep its time too
                res = prev
            else:
                t0 = time.monotonic()
                res = self._measure(m)
                durations[m] = round(time.monotonic() - t0, 1)
            parts.append(res)
            if not res.ok:
                break
        data: dict[str, Any] = {p.step: p.data for p in parts}
        data["durations_s"] = dict(self.durations[setup])
        if temp is not None:
            data["temperature_c"] = temp
        failed = [p for p in parts if not p.ok]
        if failed:
            f = failed[0]
            return self._record_setup(
                FpResult(
                    step=setup,
                    status="fail",
                    message=f"{MEASURE_LABELS[f.step]}: {f.message}",  # type: ignore[index]
                    data=data,
                )
            )
        status: Literal["pass", "skipped"] = (
            "skipped" if all(p.status == "skipped" for p in parts) else "pass"
        )
        message = "; ".join(
            f"{MEASURE_LABELS[p.step]}: {p.message}"  # type: ignore[index]
            for p in parts
            if p.status == "pass" or status == "skipped"
        )
        return self._record_setup(FpResult(step=setup, status=status, message=message, data=data))

    def skip(self, setup: FpSetup, reason: str = "") -> FpResult:
        """The operator skips a light setup (HDR channels); recorded in the report."""
        plan = self.plan[setup]
        if not plan.can_skip:
            raise InvalidRequestError(f"{plan.label} cannot be skipped")
        why = " ".join(reason.split())[:LENS_MAX_CHARS] or "no reason given"
        msg = f"skipped by the operator ({why})"
        for m in plan.measures:
            if m not in self.results or not self.results[m].ok:
                self._record(FpResult(step=m, status="skipped", message=msg))
        return self._record_setup(
            FpResult(step=setup, status="skipped", message=msg, data={"operator_skip": why})
        )

    def next_step(self) -> FpSetup | None:
        """The setup that is due (inactive setups are skipped on the way)."""
        for setup in FP_SETUPS:
            r = self.setup_results.get(setup)
            if r is None and not self.plan[setup].active:
                r = self._record_setup(
                    FpResult(step=setup, status="skipped", message=self.plan[setup].instruction)
                )
            if r is None or r.status == "fail":
                return setup
        return None
