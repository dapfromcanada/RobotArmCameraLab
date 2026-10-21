"""Camera setup procedure (Phase 10 step 5, Derek 2026-10-21).

A gated sequence; every step **passes or stops** and says what it needs from
the human. The app never guesses at causes it cannot see (iris, lighting,
focus ring). Live video is the front end's job (GUI setup window, CLI prints).

1. backup             — full settings backup (two_d/backups.py)
2. light              — two chart measurements ``LIGHT_GAP_S`` apart must agree
3. brightness         — exposure (then gain) to a chart target, inside the
                        camera range ∩ the profile's working range
4. focus              — the human turns the ring while :meth:`focus_reading`
                        reports live MTF50; :meth:`finish_focus` compares with
                        the camera + lens baseline (first run records it)
5. brightness_recheck — focus / iris may have moved
6. white_balance      — colour cameras: grey-patch channel spread
7. save               — after the human confirms: camera User Set 1 (boot set)
                        + a camera_lab preset + the setup report

Brightness is judged on a known target, not the scene: the step wedge's paper
white (``WEDGE_WHITE_TARGET``, no clipped steps) or else the grey patch
(``GREY_TARGET``). Office prints are nominal, so these are bench targets for
comparing cameras, not absolute reflectances.
"""

from __future__ import annotations

import json
import math
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from camera_lab.core.paths import safe_join, validate_serial, ws_root
from camera_lab.core.records import atomic_write_bytes
from camera_lab.two_d.errors import InvalidRequestError, TwoDError
from camera_lab.two_d.models import CameraSettingsPatch, WbChannel

if TYPE_CHECKING:
    from camera_lab.two_d.service import Actor, TwoDService
    from camera_lab.two_d.tuning import TuningRun

StepName = Literal[
    "backup", "light", "brightness", "focus", "brightness_recheck", "white_balance", "save"
]
STEPS: tuple[StepName, ...] = (
    "backup",
    "light",
    "brightness",
    "focus",
    "brightness_recheck",
    "white_balance",
    "save",
)
Status = Literal["pass", "fail", "skipped"]

LIGHT_GAP_S = 2.0
LIGHT_TOL = 0.03  # relative change between the two light readings
WEDGE_WHITE_TARGET = 0.85  # step wedge paper white, 0–1 of full scale
GREY_TARGET = 0.40  # grey patch level when no step wedge is seen
BRIGHTNESS_TOL = 0.05  # absolute, on the 0–1 level
BRIGHTNESS_MAX_ITERS = 10
RECHECK_TOL = 0.08  # absolute: re-check passes without re-tuning inside this
FOCUS_DROP_TOL = 0.10  # MTF50 more than 10 % below the baseline → drifted
WB_TOL_PCT = 2.0
WB_MAX_ITERS = 4
SETUP_STEP_BUDGET = 40


class StepResult(BaseModel):
    """Outcome of one setup step."""

    model_config = ConfigDict(frozen=True)

    step: StepName
    status: Status
    message: str
    data: dict[str, Any] = Field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status != "fail"


def setup_root() -> Path:
    """``WS/config/camera_setup`` (focus baselines per camera)."""
    return ws_root() / "config" / "camera_setup"


class SetupSession:
    """One guided setup of one open camera. Steps run in order; a failed step stops."""

    def __init__(
        self,
        service: TwoDService,
        camera_id: str,
        *,
        actor: Actor = "human",
        sleep: Callable[[float], None] = time.sleep,
        root: Path | None = None,
    ) -> None:
        self.service = service
        self.camera_id = service.resolve(camera_id)
        self.actor = actor
        self._sleep = sleep
        self._root = root
        self.results: dict[StepName, StepResult] = {}
        self.focus_best: float | None = None
        self.profile = service.get_profile(self.camera_id)
        self.mode_name, self.mode = self.profile.mode(service.current_mode(self.camera_id))
        self.run: TuningRun | None = None

    # ------------------------------------------------------------ helpers
    def _start_run(self) -> TuningRun:
        if self.run is None:
            self.run = self.service.start_run(
                self.camera_id,
                goal="camera setup procedure (backup, light, brightness, focus, WB, save)",
                max_steps=SETUP_STEP_BUDGET,
                method="setup",
                operator="agent" if self.actor == "agent" else "human",
            )
        return self.run

    def _record(self, res: StepResult) -> StepResult:
        self.results[res.step] = res
        if self.run is not None:
            self.run.recorder.log_step(
                self.actor, f"setup.{res.step}", {}, {"status": res.status, "message": res.message}
            )
        return res

    def next_step(self) -> StepName | None:
        """The first step that has not passed yet (None when all passed)."""
        for step in STEPS:
            r = self.results.get(step)
            if r is None or r.status == "fail":
                return step
        return None

    def _require(self, *steps: StepName) -> None:
        missing = [s for s in steps if s not in self.results or not self.results[s].ok]
        if missing:
            raise InvalidRequestError(f"run these setup steps first: {', '.join(missing)}")

    def _charts(self) -> dict[str, dict[str, Any]]:
        """Charts seen now, by type (first of each type)."""
        out: dict[str, dict[str, Any]] = {}
        for rec in self.service.measure_charts(self.camera_id):
            kind = rec.get("type")
            if isinstance(kind, str) and kind not in out:
                out[kind] = rec
        return out

    def _level(self, charts: dict[str, dict[str, Any]]) -> tuple[str, float, float, bool] | None:
        """(target name, level, target, clipped) from the step wedge, else the grey patch."""
        wedge = charts.get("step_wedge", {}).get("scores")
        if wedge and "levels" in wedge and wedge["levels"][-1] is not None:
            return (
                "step wedge paper white",
                float(wedge["levels"][-1]),
                WEDGE_WHITE_TARGET,
                bool(wedge.get("clipped_steps")),
            )
        grey = charts.get("grey_patch", {}).get("scores")
        if grey and grey.get("level") is not None:
            return "grey patch", float(grey["level"]), GREY_TARGET, bool(grey.get("clipped"))
        return None

    def _knobs(self) -> tuple[list[str], str | None]:
        exposure = [k for k in self.mode.knobs if k.endswith("exposure_us")]
        gain = "gain_db" if "gain_db" in self.mode.knobs else None
        return exposure, gain

    def _bounds(self, view: Any, key: str) -> tuple[float, float]:
        rng = view.ranges.get(key)
        lo, hi = float(rng.min.value), float(rng.max.value)
        wr = view.working.get(key, {})
        wlo = max(lo, float(wr["min"])) if wr.get("min") is not None else lo
        whi = min(hi, float(wr["max"])) if wr.get("max") is not None else hi
        return (wlo, whi) if wlo <= whi else (lo, hi)

    @staticmethod
    def _value(settings: Any, key: str) -> float:
        return float(settings.advanced[key] if "." in key else getattr(settings, key))

    def _patch(self, keys: list[str], value: float) -> CameraSettingsPatch:
        adv: dict[str, float | str] = {k: value for k in keys if "." in k}
        return CameraSettingsPatch(
            exposure_us=value if "exposure_us" in keys else None, advanced=adv or None
        )

    # --------------------------------------------------------------- steps
    def step_backup(self) -> StepResult:
        """Back up, then put the camera in the profile mode the setup tunes (its knobs).

        Live 2026-10-21: a TRI054S-C setup used the HDR knobs while the camera was
        in HDR-off, so its exposure changes had no effect.
        """
        b, created = self.service.backup(self.camera_id, "camera setup: before any change")
        self._start_run()
        mode = self.service.set_mode(self.camera_id, self.mode_name)
        rejected = [r for r in mode.results if r.status == "rejected"]
        if rejected:
            return self._record(
                StepResult(
                    step="backup",
                    status="fail",
                    message=f"backed up as {b.backup_id}, but mode {self.mode_name} could not "
                    f"be entered: {rejected[0].field}: {rejected[0].reason}",
                    data={"backup_id": b.backup_id},
                )
            )
        return self._record(
            StepResult(
                step="backup",
                status="pass",
                message=f"settings backed up as {b.backup_id}"
                + ("" if created else " (unchanged)")
                + f"; camera in mode {self.mode_name}",
                data={"backup_id": b.backup_id, "mode": self.mode_name},
            )
        )

    def step_light(self) -> StepResult:
        self._require("backup")
        first = self._level(self._charts())
        if first is None:
            return self._record(
                StepResult(
                    step="light",
                    status="fail",
                    message="no step wedge or grey patch in view: place one under the camera",
                )
            )
        self._sleep(LIGHT_GAP_S)
        second = self._level(self._charts())
        if second is None:
            return self._record(
                StepResult(
                    step="light",
                    status="fail",
                    message="the chart left the view during the light check",
                )
            )
        change = abs(second[1] - first[1]) / max(first[1], 1e-6)
        data = {
            "target": first[0],
            "first": round(first[1], 4),
            "second": round(second[1], 4),
            "change": round(change, 4),
        }
        if change > LIGHT_TOL:
            return self._record(
                StepResult(
                    step="light",
                    status="fail",
                    message=f"lighting is not stable ({change:.1%} change in {LIGHT_GAP_S:.0f} s): "
                    "block daylight / use steady lights",
                    data=data,
                )
            )
        return self._record(
            StepResult(
                step="light",
                status="pass",
                message=f"lighting stable ({change:.1%} change)",
                data=data,
            )
        )

    def _tune_brightness(self, step: StepName) -> StepResult:
        exp_keys, gain_key = self._knobs()
        if not exp_keys:
            return self._record(
                StepResult(
                    step=step, status="fail", message=f"mode {self.mode_name} has no exposure knob"
                )
            )
        history: list[dict[str, Any]] = []
        for _ in range(BRIGHTNESS_MAX_ITERS):
            got = self._level(self._charts())
            if got is None:
                return self._record(
                    StepResult(
                        step=step, status="fail", message="no step wedge or grey patch in view"
                    )
                )
            name, level, target, clipped = got
            view = self.service.get_settings(self.camera_id)
            exp = self._value(view.settings, exp_keys[0])
            e_lo, e_hi = self._bounds(view, exp_keys[0])
            gain = self._value(view.settings, gain_key) if gain_key else None
            history.append(
                {"exposure_us": exp, "gain_db": gain, "level": round(level, 4), "clipped": clipped}
            )
            if abs(level - target) <= BRIGHTNESS_TOL and not clipped:
                return self._record(
                    StepResult(
                        step=step,
                        status="pass",
                        message=f"{name} at {level:.2f} (target {target:.2f}) with exposure "
                        f"{exp:.0f} us" + (f", gain {gain:.1f} dB" if gain is not None else ""),
                        data={"target": name, "level": level, "history": history},
                    )
                )
            factor = 0.5 if clipped else target / max(level, 1e-3)
            if factor < 1.0 and gain_key and gain is not None:
                # too bright with gain applied: take gain off first (less noise)
                g_lo, _ = self._bounds(view, gain_key)
                if gain > g_lo + 0.05:
                    new_gain = max(g_lo, gain + 20 * math.log10(factor))
                    self.service.apply(
                        self.camera_id,
                        CameraSettingsPatch(gain_db=new_gain),
                        self.actor,
                        mode="clamp",
                    )
                    continue
            want = exp * factor
            if want > e_hi * 1.001 and exp >= e_hi * 0.999:  # exposure exhausted: gain
                if gain_key and gain is not None:
                    g_lo, g_hi = self._bounds(view, gain_key)
                    if gain < g_hi - 0.05:
                        new_gain = min(g_hi, gain + 20 * math.log10(max(factor, 1.0)))
                        self.service.apply(
                            self.camera_id,
                            CameraSettingsPatch(gain_db=new_gain),
                            self.actor,
                            mode="clamp",
                        )
                        continue
                return self._record(
                    StepResult(
                        step=step,
                        status="fail",
                        message=f"too dark: {name} at {level:.2f} (target {target:.2f}) "
                        f"at max exposure "
                        f"{e_hi:.0f} us"
                        + (f" / gain {gain:.1f} dB" if gain is not None else "")
                        + ": open the iris or add light",
                        data={"history": history},
                    )
                )
            if want < e_lo * 0.999 and exp <= e_lo * 1.001:
                if gain_key and gain is not None:
                    g_lo, _ = self._bounds(view, gain_key)
                    if gain > g_lo + 0.05:
                        self.service.apply(
                            self.camera_id,
                            CameraSettingsPatch(gain_db=g_lo),
                            self.actor,
                            mode="clamp",
                        )
                        continue
                return self._record(
                    StepResult(
                        step=step,
                        status="fail",
                        message=f"too bright: {name} at {level:.2f}"
                        + (" (clipped)" if clipped else "")
                        + f" at minimum exposure {e_lo:.0f} us: close the iris or reduce the light",
                        data={"history": history},
                    )
                )
            new_exp = min(e_hi, max(e_lo, want))
            self.service.apply(
                self.camera_id, self._patch(exp_keys, new_exp), self.actor, mode="clamp"
            )
        return self._record(
            StepResult(
                step=step,
                status="fail",
                message=f"brightness did not settle in {BRIGHTNESS_MAX_ITERS} steps "
                "(is the light changing?)",
                data={"history": history},
            )
        )

    def step_brightness(self) -> StepResult:
        self._require("backup", "light")
        return self._tune_brightness("brightness")

    # --------------------------------------------------------------- focus
    def focus_reading(self) -> dict[str, Any]:
        """One live sharpness reading (MTF50 on the resolution chart) and the best so far."""
        res = self._charts().get("resolution", {}).get("scores")
        if not res or res.get("mtf50_cyc_px") is None:
            return {
                "mtf50_cyc_px": None,
                "best": self.focus_best,
                "message": "no resolution chart in view",
            }
        value = float(res["mtf50_cyc_px"])
        self.focus_best = value if self.focus_best is None else max(self.focus_best, value)
        return {
            "mtf50_cyc_px": value,
            "mtf50_lp_mm": res.get("mtf50_lp_mm"),
            "best": self.focus_best,
            "edges": res.get("edges_measured"),
            "clipped": res.get("any_clipped"),
        }

    def _baseline_path(self) -> Path:
        return safe_join(self._root or setup_root(), f"{validate_serial(self.camera_id)}.json")

    def finish_focus(self, locked: bool) -> StepResult:
        """The human says the focus ring is set and locked (``locked=True``)."""
        self._require("backup", "light", "brightness")
        if not locked:
            return self._record(
                StepResult(
                    step="focus",
                    status="fail",
                    message="focus not confirmed: set and lock the focus ring",
                )
            )
        reading = self.focus_reading()
        value = reading["mtf50_cyc_px"]
        if value is None:
            return self._record(
                StepResult(
                    step="focus",
                    status="fail",
                    message="no resolution chart in view: place it to check focus",
                )
            )
        path = self._baseline_path()
        baseline = json.loads(path.read_text()) if path.is_file() else None
        data = {
            "mtf50_cyc_px": value,
            "mtf50_lp_mm": reading.get("mtf50_lp_mm"),
            "best_seen": self.focus_best,
        }
        if baseline is not None:
            base = float(baseline["mtf50_cyc_px"])
            data["baseline"] = base
            if value < base * (1 - FOCUS_DROP_TOL):
                return self._record(
                    StepResult(
                        step="focus",
                        status="fail",
                        message="focus has drifted or the lens was bumped: "
                        f"MTF50 {value:.3f} cycles/px is "
                        f"{1 - value / base:.0%} below the baseline {base:.3f}: refocus",
                        data=data,
                    )
                )
            return self._record(
                StepResult(
                    step="focus",
                    status="pass",
                    message=f"focus OK: MTF50 {value:.3f} (baseline {base:.3f})",
                    data=data,
                )
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "serial": self.camera_id,
            "mtf50_cyc_px": value,
            "mtf50_lp_mm": reading.get("mtf50_lp_mm"),
            "recorded_utc": datetime.now(UTC).isoformat(),
            "note": "first setup: camera + lens + working distance baseline",
        }
        atomic_write_bytes(path, json.dumps(record, indent=1).encode())
        return self._record(
            StepResult(
                step="focus",
                status="pass",
                message=f"focus recorded as the baseline: MTF50 {value:.3f} cycles/px",
                data={**data, "baseline_recorded": True},
            )
        )

    def step_brightness_recheck(self) -> StepResult:
        self._require("focus")
        got = self._level(self._charts())
        if got is not None and abs(got[1] - got[2]) <= RECHECK_TOL and not got[3]:
            return self._record(
                StepResult(
                    step="brightness_recheck",
                    status="pass",
                    message=f"brightness still OK after focusing ({got[0]} at {got[1]:.2f})",
                )
            )
        return self._tune_brightness("brightness_recheck")

    def step_white_balance(self) -> StepResult:
        self._require("brightness_recheck")
        if self.profile.characteristics.colour == "mono":
            return self._record(
                StepResult(step="white_balance", status="skipped", message="mono camera")
            )
        for _ in range(WB_MAX_ITERS + 1):
            grey = self._charts().get("grey_patch", {}).get("scores")
            if not grey or not grey.get("channel_means"):
                return self._record(
                    StepResult(
                        step="white_balance",
                        status="fail",
                        message="no grey patch in view: place it for white balance",
                    )
                )
            spread = float(grey["wb_spread_pct"])
            means = grey["channel_means"]
            if spread <= WB_TOL_PCT:
                return self._record(
                    StepResult(
                        step="white_balance",
                        status="pass",
                        message=f"grey patch neutral: channel spread {spread:.1f} %",
                        data={"channel_means": means},
                    )
                )
            wb = self.service.get_settings(self.camera_id).settings.white_balance
            if wb is None:
                return self._record(
                    StepResult(
                        step="white_balance",
                        status="fail",
                        message="camera has no white balance control",
                    )
                )
            g = float(means["green"])
            new: dict[WbChannel, float] = {
                "red": wb["red"] * g / max(float(means["red"]), 1e-6),
                "green": wb["green"],
                "blue": wb["blue"] * g / max(float(means["blue"]), 1e-6),
            }
            self.service.apply(
                self.camera_id, CameraSettingsPatch(white_balance=new), self.actor, mode="clamp"
            )
        return self._record(
            StepResult(
                step="white_balance",
                status="fail",
                message=f"white balance did not reach {WB_TOL_PCT} % in {WB_MAX_ITERS} steps "
                "(mixed light?)",
            )
        )

    # ---------------------------------------------------------------- save
    def save(self, confirm: bool, preset_name: str | None = None) -> StepResult:
        """Write User Set 1 (boot set) + preset + report. Needs the human's confirmation."""
        self._require(
            "backup", "light", "brightness", "focus", "brightness_recheck", "white_balance"
        )
        if confirm is not True:
            return self._record(
                StepResult(
                    step="save", status="fail", message="not saved: the human did not confirm"
                )
            )
        run = self._start_run()
        try:
            [cap] = self.service.capture(
                self.camera_id, 1, run.run_id, "camera setup result", self.actor
            )
            user_set = self.service.save_user_set(
                self.camera_id, "UserSet1", confirm=True, actor=self.actor
            )
        except TwoDError as exc:  # keep the camera's exact answer in the run and report
            res = self._record(
                StepResult(step="save", status="fail", message=f"save failed: {exc}")
            )
            self.write_report()
            return res
        name = preset_name or f"setup-{datetime.now(UTC):%Y%m%d}"
        not_stored = self._not_stored(user_set.get("stored_features"))
        notes = list(user_set.get("notes", []))
        if not_stored:
            notes.append(
                "not stored in the user set (camera_lab presets keep them): "
                + ", ".join(not_stored)
            )
        res = self._record(
            StepResult(
                step="save",
                status="pass",
                message=f"saved to {user_set['slot']}"
                + (f" (camera boots with {user_set['default']})" if user_set.get("default") else "")
                + f" and preset {name}",
                data={
                    "user_set": user_set,
                    "preset": name,
                    "capture_id": cap.capture_id,
                    "notes": notes,
                },
            )
        )
        self.write_report()
        self.service.finish_run(
            run.run_id,
            cap.capture_id,
            preset_name=name,
            summary="camera setup passed and saved",
            actor=self.actor,
            overwrite=True,
        )
        return res

    def _not_stored(self, stored: list[str] | None) -> list[str]:
        if stored is None:
            return []
        adv = self.service.get_settings(self.camera_id).settings.advanced
        wanted = set()
        if any(k.startswith("hdr.") for k in adv):
            wanted |= {"HDRTuningEnable", "HDRChannelExposureTime", "HDRChannelAnalogGain"}
        wanted |= {"ExposureTime", "Gain", "BalanceRatio", "PixelFormat"}
        return sorted(w for w in wanted if w not in stored)

    # -------------------------------------------------------------- report
    def report(self) -> dict[str, Any]:
        return {
            "camera_id": self.camera_id,
            "mode": self.mode_name,
            "run_id": self.run.run_id if self.run else None,
            "steps": [self.results[s].model_dump(mode="json") for s in STEPS if s in self.results],
            "next_step": self.next_step(),
        }

    def write_report(self) -> Path | None:
        if self.run is None:
            return None
        path = safe_join(self.run.recorder.run_dir, "setup_report.json")
        atomic_write_bytes(path, json.dumps(self.report(), indent=1, default=str).encode())
        return path
