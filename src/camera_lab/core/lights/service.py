"""LightService: applies the safety limits on top of any LightController.

Every light change in camera_lab (CLI, MCP, GUI, classic tuner) goes through
:meth:`LightService.set_levels`:

- each requested level is checked against ``[0, min(hardware max, lights.*.max_pct)]``;
  out-of-range or unknown channels are **rejected** (not clamped), with a reason
  naming the YAML key;
- accepted changes are **ramped**: no channel moves more than its
  ``max_step_pct`` per change, and changes are paced by a token bucket at
  ``max_changes_per_s`` (ramp steps count as changes);
- :meth:`all_off` is immediate (turning lights off is always safe) and not
  rate-limited.

:func:`install_lights_off_handlers` turns the lights off at normal exit, on an
unhandled exception (``atexit``) and on SIGINT/SIGTERM.
"""

from __future__ import annotations

import atexit
import math
import os
import signal
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import FrameType, TracebackType
from typing import Any, Literal

from camera_lab.core.lights.base import LightChannel, LightController
from camera_lab.core.logging import get_logger
from camera_lab.core.safety import (
    LoadedSafetyLimits,
    RateLimiter,
    Rejected,
    ResolvedLightLimits,
    check,
)

_log = get_logger(__name__)


@dataclass(frozen=True)
class LightChannelResult:
    """Outcome for one requested channel."""

    channel: str
    status: Literal["applied", "rejected"]
    requested: Any
    value: float | None
    reason: str | None = None


@dataclass(frozen=True)
class LightSetResult:
    """Outcome of :meth:`LightService.set_levels`."""

    results: tuple[LightChannelResult, ...]
    ramp_steps: int
    levels: dict[str, float]

    @property
    def ok(self) -> bool:
        """True if nothing was rejected."""
        return all(r.status == "applied" for r in self.results)


class LightService:
    """Safety layer over a :class:`LightController`. Thread-safe."""

    def __init__(
        self,
        controller: LightController,
        limits: LoadedSafetyLimits,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.controller = controller
        self._channels = {c.id: c for c in controller.channels()}
        self._limits: dict[str, ResolvedLightLimits] = {
            cid: limits.light(cid) for cid in self._channels
        }
        rate = min(lim.max_changes_per_s for lim in self._limits.values())
        self._rate = RateLimiter(rate, clock=clock)
        self._sleep = sleep
        self._lock = threading.Lock()

    def channels(self) -> list[LightChannel]:
        """Return the controller's channels."""
        return list(self._channels.values())

    def limits_for(self, channel_id: str) -> ResolvedLightLimits:
        """Return the resolved safety limits of one channel."""
        return self._limits[channel_id]

    def get_levels(self) -> dict[str, float]:
        """Return the current levels (%)."""
        return self.controller.get_levels()

    def _validate(self, cid: Any, pct: Any) -> LightChannelResult:
        if not isinstance(cid, str) or cid not in self._channels:
            known = ", ".join(self._channels)
            return LightChannelResult(
                str(cid)[:64], "rejected", pct, None, f"unknown light channel; known: {known}"
            )
        rng = self._limits[cid].level_range(self._channels[cid].max_pct)
        res = check(pct, rng, mode="reject")
        if isinstance(res, Rejected):
            return LightChannelResult(cid, "rejected", pct, None, res.reason)
        # Lights have no increment, so a non-rejected result is Accepted.
        return LightChannelResult(cid, "applied", pct, float(res.value))

    def set_levels(self, levels: Mapping[Any, Any]) -> LightSetResult:
        """Validate, then ramp the accepted channels to their targets.

        Rejected channels are left unchanged; accepted ones are applied.
        """
        results = tuple(self._validate(cid, pct) for cid, pct in levels.items())
        targets = {r.channel: r.value for r in results if r.value is not None}
        steps = 0
        with self._lock:
            current = self.controller.get_levels()
            while True:
                nxt: dict[str, float] = {}
                for cid, target in targets.items():
                    cur = current[cid]
                    if math.isclose(cur, target, abs_tol=1e-9):
                        continue
                    step = self._limits[cid].max_step_pct
                    nxt[cid] = min(target, cur + step) if target > cur else max(target, cur - step)
                if not nxt:
                    break
                self._rate.acquire(self._sleep)
                self.controller.set_levels(nxt)
                current.update(nxt)
                steps += 1
        final = self.controller.get_levels()
        for r in results:
            if r.status == "rejected":
                _log.warning("light_level_rejected", channel=r.channel, reason=r.reason)
        _log.info("light_levels_set", levels=final, ramp_steps=steps)
        return LightSetResult(results, steps, final)

    def all_off(self) -> None:
        """Turn every channel off immediately (no ramp, no rate limit)."""
        self.controller.all_off()

    def close(self) -> None:
        """Turn off and close the controller."""
        self.controller.close()

    def __enter__(self) -> LightService:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


def install_lights_off_handlers(controller: LightController) -> Callable[[], None]:
    """Turn the lights off at exit (``atexit``) and on SIGINT/SIGTERM.

    Signal handlers are installed only from the main thread. After turning the
    lights off, a signal is passed on to the previous handler (or re-raised with
    the default action). Returns a function that uninstalls the handlers.
    """

    def lights_off() -> None:
        try:
            controller.all_off()
        except Exception:  # never let shutdown fail because of the lights
            _log.exception("lights_off_failed")

    atexit.register(lights_off)
    previous: dict[int, Any] = {}

    def on_signal(signum: int, frame: FrameType | None) -> None:
        lights_off()
        prev = previous.get(signum, signal.SIG_DFL)
        signal.signal(signum, prev)
        if callable(prev):
            prev(signum, frame)
        elif prev == signal.SIG_DFL:
            os.kill(os.getpid(), signum)

    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous[sig] = signal.getsignal(sig)
            signal.signal(sig, on_signal)

    def uninstall() -> None:
        atexit.unregister(lights_off)
        for sig, prev in previous.items():
            if signal.getsignal(sig) is on_signal:
                signal.signal(sig, prev)

    return uninstall
