"""Tuning strategies, chosen per camera and mode by the camera profile.

A strategy is a registered class with ``run(...) -> TuneResult``. Every strategy
works through ``TwoDService`` (safety limits, step budget, run records) and
never touches the lights. Add one with ``@STRATEGIES.register("name")``.

Built-ins (all non-AI, built on :class:`~camera_lab.two_d.tuning.ClassicTuner`):

- ``linear``: exposure (pedestal-aware secant) → gain → grey-world white balance.
- ``prism``: ``linear`` for 3-sensor prism cameras; white balance by per-sensor
  gain (``options.wb_method: gain``) or per-sensor exposure (``exposure``).
- ``hdr``: tunes the mode's HDR exposure knobs together (no linear model:
  proportional first step, then bisection on clipping); never raises gain.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, ClassVar, Literal

from camera_lab.core.registry import Registry
from camera_lab.two_d.errors import InvalidRequestError
from camera_lab.two_d.profiles import ModeProfile
from camera_lab.two_d.tuning import ClassicTuner, TuneResult

if TYPE_CHECKING:
    from camera_lab.two_d.service import TwoDService


class Strategy(ABC):
    """Tunes one camera in one mode."""

    name: ClassVar[str]

    @abstractmethod
    def build(
        self, service: TwoDService, camera_id: str, mode: ModeProfile, **kw: Any
    ) -> ClassicTuner:
        """Return the configured tuner for this camera and mode."""

    def run(
        self,
        service: TwoDService,
        camera_id: str,
        mode: ModeProfile,
        *,
        max_steps: int = 15,
        target_mean: float | None = None,
        white_balance: bool = False,
        operator: Literal["human", "agent"] = "human",
        actor: str = "classic",
    ) -> TuneResult:
        """Run the strategy in a new ``classic`` run."""
        tuner = self.build(
            service,
            camera_id,
            mode,
            max_steps=max_steps,
            target_mean=target_mean,
            white_balance=white_balance,
            operator=operator,
            actor=actor,
        )
        return tuner.run()


STRATEGIES: Registry[type[Strategy]] = Registry("tuning strategy")


@STRATEGIES.register("linear")
class LinearStrategy(Strategy):
    """Exposure → gain → grey-world WB by gain."""

    name: ClassVar[str] = "linear"

    def build(
        self, service: TwoDService, camera_id: str, mode: ModeProfile, **kw: Any
    ) -> ClassicTuner:
        """Classic tuner on ``exposure_us``."""
        return ClassicTuner(service, camera_id, label="linear", **kw)


@STRATEGIES.register("prism")
class PrismStrategy(Strategy):
    """Like ``linear``; white balance by per-sensor gain or per-sensor exposure."""

    name: ClassVar[str] = "prism"

    def build(
        self, service: TwoDService, camera_id: str, mode: ModeProfile, **kw: Any
    ) -> ClassicTuner:
        """Classic tuner with ``options.wb_method``."""
        method = mode.options.get("wb_method", "gain")
        if method not in ("gain", "exposure"):
            raise InvalidRequestError(
                f"prism wb_method must be 'gain' or 'exposure', not {method!r}"
            )
        return ClassicTuner(service, camera_id, wb_method=method, label="prism", **kw)


@STRATEGIES.register("hdr")
class HdrStrategy(Strategy):
    """Tune the HDR exposure knobs together; no linear model, no gain stage."""

    name: ClassVar[str] = "hdr"

    def build(
        self, service: TwoDService, camera_id: str, mode: ModeProfile, **kw: Any
    ) -> ClassicTuner:
        """Classic tuner on the mode's ``*.exposure_us`` advanced knobs."""
        keys = tuple(k for k in mode.knobs if "." in k and k.endswith("exposure_us"))
        if not keys:
            raise InvalidRequestError(
                "hdr strategy needs advanced '*.exposure_us' knobs in the mode"
            )
        return ClassicTuner(
            service, camera_id, exposure_keys=keys, linear=False, use_gain=False, label="hdr", **kw
        )


__all__ = ["STRATEGIES", "HdrStrategy", "LinearStrategy", "PrismStrategy", "Strategy"]
