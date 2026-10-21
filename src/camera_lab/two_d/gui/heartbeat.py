"""UI heartbeat: measures the longest stall of the Qt event loop.

A timer fires every :data:`HEARTBEAT_INTERVAL_MS`; the extra time between two
firings (beyond the interval) is a UI stall. The spec allows no stall above
:data:`STALL_LIMIT_MS`; longer stalls are logged as warnings (structlog, stderr)
and the maximum is logged when the heartbeat stops.
"""

from __future__ import annotations

from PySide6.QtCore import QElapsedTimer, QObject, QTimer

from camera_lab.core.logging import get_logger
from camera_lab.two_d.gui.constants import HEARTBEAT_INTERVAL_MS, STALL_LIMIT_MS

_log = get_logger(__name__)
NS_PER_MS = 1_000_000


class UiHeartbeat(QObject):
    """Measures the longest UI-thread stall; read it with :attr:`max_stall_ms`."""

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._max_stall_ms = 0.0
        self._ticks = 0
        self._elapsed = QElapsedTimer()
        self._timer = QTimer(self)
        self._timer.setInterval(HEARTBEAT_INTERVAL_MS)
        self._timer.timeout.connect(self._on_heartbeat_timer_timeout)

    @property
    def max_stall_ms(self) -> float:
        """The longest stall seen since :meth:`start` / :meth:`reset` (milliseconds)."""
        return self._max_stall_ms

    @property
    def ticks(self) -> int:
        """Number of heartbeats measured."""
        return self._ticks

    @property
    def running(self) -> bool:
        """True while the heartbeat timer is active."""
        return self._timer.isActive()

    def start(self) -> None:
        """Start (or restart) measuring."""
        self.reset()
        self._elapsed.start()
        self._timer.start()

    def reset(self) -> None:
        """Forget the maximum seen so far."""
        self._max_stall_ms = 0.0
        self._ticks = 0
        self._elapsed.restart()

    def stop(self) -> None:
        """Stop measuring and log the maximum stall."""
        if self._timer.isActive():
            self._timer.stop()
            _log.info(
                "ui_heartbeat",
                max_stall_ms=round(self._max_stall_ms, 1),
                ticks=self._ticks,
                limit_ms=STALL_LIMIT_MS,
            )

    def _on_heartbeat_timer_timeout(self) -> None:
        gap_ms = self._elapsed.nsecsElapsed() / NS_PER_MS
        self._elapsed.restart()
        stall_ms = max(0.0, gap_ms - HEARTBEAT_INTERVAL_MS)
        self._ticks += 1
        self._max_stall_ms = max(self._max_stall_ms, stall_ms)
        if stall_ms > STALL_LIMIT_MS:
            _log.warning("ui_stall", stall_ms=round(stall_ms, 1), limit_ms=STALL_LIMIT_MS)
