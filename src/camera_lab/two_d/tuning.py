"""Tuning runs (step-budgeted) and the classic, non-AI auto-exposure tuner.

:class:`TuningRun` wraps a :class:`RunRecorder` and a :class:`StepBudget`.
Every capture call, settings change or light change made while a run is
active consumes one step (before the action). When the budget is used up the
action is refused with :class:`StepBudgetExhausted` and nothing is done.

:class:`ClassicTuner` is the reference baseline Claude is compared against:

1. Exposure search: the first step is proportional (``exposure × target / mean``);
   after that a secant on ``mean_level`` vs ``exposure`` (``mean = pedestal +
   k · exposure``, so a black-level pedestal such as the JAI's ~3 % does not
   slow it down), kept inside a bracket of known too-dark / too-bright
   exposures, with geometric bisection as fallback. Very dark frames jump ×100;
   clipped frames count as too bright.
2. If exposure is at its effective maximum and the image is still too dark,
   gain is raised in steps of at most 3 dB.
3. If the image is too bright at minimum exposure, gain is lowered to its
   minimum; if it is still too bright the run stops with "needs less light".
4. Optional grey-world white balance (colour formats only): red/blue ratios are
   scaled so the mean signal above the black-level pedestal is equal in R, G and
   B on the scoring ROI (pedestal from one minimum-exposure frame), then verified.
5. Converged when ``|mean − target| ≤ 0.05 × target`` and
   ``clipped_pct ≤ clip_limit_pct`` (default 0.5 %, success criterion 5).
   **Clip-limited**: if the bracket closes (too-bright/too-dark exposures within
   ``CLIP_LIMIT_BRACKET`` of each other) because highlights clip before the mean
   reaches the target, the scene cannot meet both goals; the tuner settles on
   the brightest non-clipping exposure and stops (``stop_reason =
   "clip_limited"``). Otherwise it stops when the step budget is used up.
6. The best capture is the one with the highest composite score.

Settings are applied with ``mode="clamp"``: out-of-range requests are clamped
to the effective range and the clamp is logged in ``steps.jsonl``.
**The classic tuner never touches the lights**: lighting is the agent's or the
human's choice in v1.
"""

from __future__ import annotations

import contextlib
import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

import numpy as np
import numpy.typing as npt

from camera_lab.core.records import RunRecord, RunRecorder
from camera_lab.core.safety import BudgetExhaustedError, EffectiveRange, StepBudget
from camera_lab.two_d.errors import InvalidRequestError, StepBudgetExhausted
from camera_lab.two_d.models import CameraSettingsPatch, is_bayer, is_rgb
from camera_lab.two_d.processing import demosaic
from camera_lab.two_d.scoring import ScoringParams, crop_roi

if TYPE_CHECKING:
    from camera_lab.two_d.service import CaptureResult, TwoDService


class TuningRun:
    """An active, step-budgeted run on one camera."""

    def __init__(
        self,
        recorder: RunRecorder,
        budget: StepBudget,
        camera_id: str,
        method: Literal["agent", "classic", "manual", "setup"],
    ) -> None:
        self.recorder = recorder
        self.budget = budget
        self.camera_id = camera_id
        self.method = method

    @property
    def run_id(self) -> str:
        """The run id."""
        return self.recorder.run_id

    @property
    def steps_used(self) -> int:
        """Steps consumed so far."""
        return self.budget.used

    @property
    def steps_remaining(self) -> int:
        """Steps left."""
        return self.budget.remaining

    def use_step(self) -> None:
        """Consume one step before an action.

        Raises:
            StepBudgetExhausted: no steps left (the action must not be performed).
        """
        try:
            self.budget.use()
        except BudgetExhaustedError:
            raise StepBudgetExhausted(
                f"run {self.run_id}: step budget exhausted "
                f"({self.budget.used}/{self.budget.max_steps} steps used)"
            ) from None

    def log(
        self,
        actor: str,
        tool: str,
        arguments: dict[str, Any] | None = None,
        result: Any = None,
        rejections: list[str] | tuple[str, ...] = (),
    ) -> None:
        """Append one line to the run's ``steps.jsonl``."""
        self.recorder.log_step(actor, tool, arguments, result, rejections)

    def finish(
        self, *, best_capture_id: str | None, summary: str = "", preset_saved: str | None = None
    ) -> RunRecord:
        """Write the final ``run.json``."""
        return self.recorder.finish(
            best_capture_id=best_capture_id,
            summary=summary,
            preset_saved=preset_saved,
            steps_used=self.budget.used,
        )


# ============================================================ classic tuner
@dataclass
class TuneResult:
    """Outcome of :meth:`ClassicTuner.run`."""

    run_id: str
    converged: bool
    steps_used: int
    best_capture_id: str | None
    best_composite: float | None
    message: str
    stop_reason: str = "budget"  # converged | clip_limited | too_dark | too_bright | budget
    mode: str | None = None  # profile mode (set by TwoDService.tune)
    strategy: str | None = None
    final_mean: float | None = None
    final_clipped_pct: float | None = None
    history: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """JSON-able dict."""
        return {
            "run_id": self.run_id,
            "converged": self.converged,
            "steps_used": self.steps_used,
            "best_capture_id": self.best_capture_id,
            "best_composite": self.best_composite,
            "message": self.message,
            "stop_reason": self.stop_reason,
            "mode": self.mode,
            "strategy": self.strategy,
            "final_mean": self.final_mean,
            "final_clipped_pct": self.final_clipped_pct,
            "history": self.history,
        }


DARK_MEAN = 0.005  # below this the frame carries no exposure information
CLIP_LIMIT_BRACKET = 1.10  # hi/lo exposure ratio at which a clip-limited search stops
DARK_JUMP = 100.0
MAX_GAIN_STEP_DB = 3.0


class ClassicTuner:
    """Non-AI auto exposure / gain (+ optional grey-world WB) on one open camera."""

    def __init__(
        self,
        service: TwoDService,
        camera_id: str,
        *,
        target_mean: float | None = None,
        max_steps: int = 15,
        white_balance: bool = False,
        clip_limit_pct: float = 0.5,
        operator: Literal["human", "agent"] = "human",
        actor: str = "classic",
        exposure_keys: tuple[str, ...] = ("exposure_us",),
        linear: bool = True,
        use_gain: bool = True,
        wb_method: Literal["gain", "exposure"] = "gain",
        label: str = "classic",
    ) -> None:
        params = service.scoring_params(camera_id)
        if target_mean is not None:
            if not 0.1 <= target_mean <= 0.9:
                raise InvalidRequestError("target_mean must be within 0.1..0.9")
            params = params.model_copy(update={"target_mean": float(target_mean)})
        if not 0 < clip_limit_pct <= 100:
            raise InvalidRequestError("clip_limit_pct must be within (0, 100]")
        self.service = service
        self.camera_id = camera_id
        self.params: ScoringParams = params
        self.max_steps = max_steps
        self.white_balance = white_balance
        self.clip_limit_pct = clip_limit_pct
        self.operator = operator
        self.actor = actor
        if not exposure_keys:
            raise InvalidRequestError("at least one exposure key is needed")
        self.exposure_keys = exposure_keys
        self.linear = linear
        self.use_gain = use_gain
        self.wb_method = wb_method
        self.label = label

    # ------------------------------------------------------------- helpers
    def _converged(self, mean: float, clipped: float) -> bool:
        t = self.params.target_mean
        return abs(mean - t) <= 0.05 * t and clipped <= self.clip_limit_pct

    def _too_bright(self, mean: float, clipped: float) -> bool:
        return mean > self.params.target_mean or clipped > self.clip_limit_pct

    @staticmethod
    def _bounds(rng: Any) -> tuple[float, float]:
        if not isinstance(rng, EffectiveRange):
            raise InvalidRequestError(f"{getattr(rng, 'reason', 'setting not adjustable')}")
        return rng.min.value, rng.max.value

    def _next_exposure(
        self,
        exp: float,
        mean: float,
        clipped: float,
        points: list[tuple[float, float]],
        lo: float | None,
        hi: float | None,
        hi_clipped: bool = False,
    ) -> float:
        target = self.params.target_mean
        if mean < DARK_MEAN:
            guess = exp * DARK_JUMP
        elif len(points) >= 2 and points[-1][0] != points[-2][0]:
            (e1, m1), (e2, m2) = points[-2], points[-1]
            k = (m2 - m1) / (e2 - e1)  # linear sensor: mean = pedestal + k · exposure
            guess = e2 + (target - m2) / k if k > 0 else exp * target / mean
        else:
            guess = exp * target / mean
        if clipped > self.clip_limit_pct:
            # clipped while the mean is still low: bisect towards the last good exposure
            guess = math.sqrt(lo * exp) if lo is not None and lo < exp else exp * 0.5
        if lo is not None and hi is not None and hi_clipped:
            # the upper bound clips: the mean model says nothing about clipping,
            # so never aim past the geometric midpoint of the bracket
            guess = min(guess, math.sqrt(lo * hi))
        # stay inside the bracket; bisect geometrically if the guess leaves it
        if lo is not None and hi is not None and not lo < guess < hi:
            guess = math.sqrt(lo * hi)
        elif lo is not None and guess <= lo:
            guess = lo * 2
        elif hi is not None and guess >= hi:
            guess = hi / 2
        return guess

    def _exposure_patch(self, value: float) -> CameraSettingsPatch:
        if self.exposure_keys == ("exposure_us",):
            return CameraSettingsPatch(exposure_us=value)
        return CameraSettingsPatch(advanced=dict.fromkeys(self.exposure_keys, value))

    def _exposure_of(self, s: Any) -> float:
        if self.exposure_keys == ("exposure_us",):
            return float(s.exposure_us)
        return float(s.advanced[self.exposure_keys[0]])

    def _exposure_bounds(self, ranges: dict[str, Any]) -> tuple[float, float]:
        lows, highs = [], []
        for key in self.exposure_keys:
            lo, hi = self._bounds(ranges.get(key))
            lows.append(lo)
            highs.append(hi)
        if max(lows) > min(highs):
            raise InvalidRequestError("exposure knobs have no common range")
        return max(lows), min(highs)

    @staticmethod
    def _steer(
        bounds: tuple[float, float], view: Any, keys: tuple[str, ...]
    ) -> tuple[float, float]:
        """Narrow camera bounds to the profile working range (guidance; keeps a valid range)."""
        lo, hi = bounds
        for key in keys:
            wr = view.working.get(key, {})
            if wr.get("min") is not None:
                lo = max(lo, float(wr["min"]))
            if wr.get("max") is not None:
                hi = min(hi, float(wr["max"]))
        return (lo, hi) if lo <= hi else bounds

    def _capture(self, run_id: str) -> CaptureResult:
        [res] = self.service.capture(self.camera_id, 1, run_id, "classic tune", self.actor)
        return res

    def _apply(self, patch: CameraSettingsPatch) -> None:
        self.service.apply(self.camera_id, patch, self.actor, mode="clamp")

    # ----------------------------------------------------------------- run
    def run(self) -> TuneResult:
        """Run the tuner in a new ``classic`` run and finish it."""
        svc = self.service
        run = svc.start_run(
            self.camera_id,
            goal=f"{self.label} auto-exposure to mean {self.params.target_mean}",
            max_steps=self.max_steps,
            method="classic",
            operator=self.operator,
        )
        view = svc.get_settings(self.camera_id)
        e_min, e_max = self._steer(self._exposure_bounds(view.ranges), view, self.exposure_keys)
        g_rng = view.ranges.get("gain_db")
        g_min, g_max = (
            self._steer(self._bounds(g_rng), view, ("gain_db",))
            if g_rng is not None
            else (0.0, 0.0)
        )
        captures: list[tuple[str, float]] = []
        history: list[dict[str, Any]] = []
        points: list[tuple[float, float]] = []
        lo = hi = None
        lo_mean = lo_clipped = 0.0
        hi_clipped = False
        converged = False
        stop_reason = "budget"
        message = "step budget used up"
        mean = clipped = None
        try:
            while True:
                cap = self._capture(run.run_id)
                s = cap.settings
                mean = cap.scores.metric("mean_level")
                clipped = cap.scores.metric("clipped_pct")
                captures.append((cap.capture_id, cap.scores.composite))
                history.append(
                    {
                        "capture_id": cap.capture_id,
                        "exposure_us": self._exposure_of(s),
                        "gain_db": s.gain_db,
                        "mean_level": mean,
                        "clipped_pct": clipped,
                    }
                )
                if self._converged(mean, clipped):
                    converged, message, stop_reason = True, "converged", "converged"
                    break
                exp, gain = self._exposure_of(s), s.gain_db
                if (
                    self.linear
                    and mean >= DARK_MEAN
                    and clipped <= self.clip_limit_pct
                    and mean < 0.98
                ):
                    points.append((exp, mean))
                if self._too_bright(mean, clipped):
                    if hi is None or exp <= hi:
                        hi, hi_clipped = exp, clipped > self.clip_limit_pct
                elif lo is None or exp >= lo:
                    lo, lo_mean, lo_clipped = exp, mean, clipped
                if (
                    lo is not None
                    and hi is not None
                    and hi_clipped
                    and hi / lo <= CLIP_LIMIT_BRACKET
                    and lo_mean < self.params.target_mean
                ):
                    stop_reason = "clip_limited"
                    message = (
                        f"clip-limited: highlights clip before the target mean "
                        f"(best mean {lo_mean:.3f} at {lo:.0f} us)"
                    )
                    mean, clipped = lo_mean, lo_clipped
                    if abs(exp - lo) > 1e-6:  # leave the camera at the best exposure
                        with contextlib.suppress(StepBudgetExhausted):
                            self._apply(self._exposure_patch(lo))
                    break
                bright = self._too_bright(mean, clipped)
                if not bright and exp >= e_max * 0.999:
                    if not self.use_gain or gain >= g_max - 1e-9:
                        message = "too dark at maximum exposure and gain: needs more light"
                        stop_reason = "too_dark"
                        break
                    need = 20 * math.log10(self.params.target_mean / max(mean, 1e-6))
                    step = min(MAX_GAIN_STEP_DB, max(0.1, need))
                    self._apply(CameraSettingsPatch(gain_db=min(g_max, gain + step)))
                    lo = hi = None
                    points.clear()
                    continue
                if bright and exp <= e_min * 1.001:
                    if self.use_gain and gain > g_min + 1e-9:
                        self._apply(CameraSettingsPatch(gain_db=g_min))
                        lo = hi = None
                        points.clear()
                        continue
                    message = "too bright at minimum exposure and gain: needs less light"
                    stop_reason = "too_bright"
                    break
                new_exp = self._next_exposure(exp, mean, clipped, points, lo, hi, hi_clipped)
                new_exp = min(max(new_exp, e_min), e_max)
                self._apply(self._exposure_patch(new_exp))
            settled = stop_reason in ("converged", "clip_limited")
            if settled and self.white_balance and self._is_colour():
                ok, wb_message = self._grey_world(run.run_id, captures, history)
                if stop_reason == "converged":
                    converged, message = ok, wb_message
                else:
                    message = f"{message}; white balance applied"
        except StepBudgetExhausted:
            if stop_reason == "budget":
                message = "step budget used up"
        best = max(captures, key=lambda c: c[1]) if captures else None
        summary = f"classic tuner: {message}; {run.steps_used} steps"
        svc.finish_run(
            run.run_id,
            best_capture_id=best[0] if best else None,
            summary=summary,
            actor=self.actor,
        )
        return TuneResult(
            run_id=run.run_id,
            converged=converged,
            steps_used=run.steps_used,
            best_capture_id=best[0] if best else None,
            best_composite=best[1] if best else None,
            message=message,
            stop_reason=stop_reason,
            final_mean=mean,
            final_clipped_pct=clipped,
            history=history,
        )

    def _is_colour(self) -> bool:
        fmt = self.service.get_settings(self.camera_id).settings.pixel_format
        return is_bayer(fmt) or is_rgb(fmt)

    def _channel_means(self) -> npt.NDArray[np.float64]:
        """Per-channel means of a fresh, non-recording grab (uses no run step)."""
        frame = self.service.grab(self.camera_id, timeout_ms=10_000)
        rgb = crop_roi(demosaic(frame.data, frame.pixel_format), self.params.roi)
        means: npt.NDArray[np.float64] = rgb.reshape(-1, 3).astype(np.float64).mean(axis=0)
        return means

    def _grey_world(
        self, run_id: str, captures: list[tuple[str, float]], history: list[dict[str, Any]]
    ) -> tuple[bool, str]:
        """Scale red/blue so the mean signal R = G = B, then verify with one capture.

        Measurement frames use the non-recording grab (no run steps). The signal
        is measured above the black-level pedestal: one frame at the minimum
        exposure gives the per-channel pedestal (the JAI shows ~3 % of
        full scale, which is not scaled by the white-balance gains; ignoring it
        under-corrects the weak channel).
        """
        view = self.service.get_settings(self.camera_id)
        settings = view.settings
        lit = self._channel_means()
        exposure = self._exposure_of(settings)
        dark_exp, _ = self._exposure_bounds(view.ranges)
        self._apply(self._exposure_patch(dark_exp))
        pedestal = self._channel_means()
        self._apply(self._exposure_patch(exposure))
        r, g, b = (float(x) for x in lit - pedestal)
        if min(r, g, b) <= 0:
            return True, "converged (white balance skipped: a channel has no signal)"
        wb = settings.white_balance or {"red": 1.0, "green": 1.0, "blue": 1.0}
        if self.wb_method == "exposure":
            # prism camera: balance with per-sensor exposure instead of gain (no gain
            # noise). Red/blue gains go back to ×1; their current effect is folded into
            # the exposure ratios (signal at unity gain = measured signal / gain).
            patch = CameraSettingsPatch(
                white_balance={"red": 1.0, "blue": 1.0},
                advanced={
                    "prism.exposure_mode": "individual",
                    "prism.exposure_red_us": exposure * g * wb["red"] / r,
                    "prism.exposure_green_us": exposure,
                    "prism.exposure_blue_us": exposure * g * wb["blue"] / b,
                },
            )
        else:
            patch = CameraSettingsPatch(
                white_balance={"red": wb["red"] * g / r, "blue": wb["blue"] * g / b}
            )
        self._apply(patch)
        cap = self._capture(run_id)
        mean = cap.scores.metric("mean_level")
        clipped = cap.scores.metric("clipped_pct")
        captures.append((cap.capture_id, cap.scores.composite))
        history.append(
            {
                "capture_id": cap.capture_id,
                "white_balance": cap.settings.white_balance,
                "wb_method": self.wb_method,
                "pedestal": [round(float(x), 3) for x in pedestal],
                "mean_level": mean,
                "clipped_pct": clipped,
            }
        )
        ok = self._converged(mean, clipped)
        return ok, "converged with white balance" if ok else "white balance moved the exposure"
