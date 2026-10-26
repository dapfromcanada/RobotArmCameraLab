"""The "Focus" window: live video and a live sharpness meter on the resolution chart
(Derek 2026-10-26: "a focus tool, that way we can see what is happening").

All camera work runs on the :class:`CameraWorker` thread (``focus_*`` jobs): one reading at a
time, the next one asked for as soon as the last came back. The window shows MTF50 per colour
plane, the best so far (it starts again by itself when the chart moves), a hint for the focus
ring, and the warnings of the meter (chart too small, light dropped, clipped, too dark).
"""

from __future__ import annotations

from contextlib import suppress
from typing import Any

from PySide6.QtCore import QTimer, Signal
from PySide6.QtGui import QImage
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from camera_lab.two_d.focus import FocusReading
from camera_lab.two_d.gui.widgets.live_view_widget import LiveViewWidget
from camera_lab.two_d.gui.workers import CameraWorker, FocusToolOutcome

MARGIN = 10
SPACING = 8
PANEL_WIDTH = 380
WINDOW_WIDTH = 1200
WINDOW_HEIGHT = 760
READ_INTERVAL_MS = 150  # pause between readings (each reading takes ≈ 0.3–1 s)

WINDOW_TITLE = "Focus"
TITLE_TEXT = "Sharpness (MTF50, cycles per pixel; higher is sharper, 0.5 is the limit)"
WAITING_TEXT = "starting..."
NO_VALUE_TEXT = "-"
RESET_TEXT = "Start best again"
AUTO_TEXT = "Exposure follows the light"
MOVED_TEXT = "chart moved: best starts again"
FOCUS_JOBS = frozenset({"focus_reading", "focus_reset"})


def plane_text(reading: FocusReading) -> str:
    """'R 0.280  G 0.307  B 0.249' (or the mono plane)."""
    parts = [
        f"{name} {value:.3f}" if value is not None else f"{name} {NO_VALUE_TEXT}"
        for name, value in reading.mtf50.items()
    ]
    return "   ".join(parts)


def info_text(reading: FocusReading) -> str:
    out = [
        f"exposure {reading.exposure_us / 1000:.1f} ms",
        f"highlights {reading.highlights * 100:.0f} %",
        f"clipped {reading.clipped_pct:.2f} %",
    ]
    if reading.found:
        out.append(f"edges {reading.edges}/{reading.edges_total}")
        if reading.px_per_mm is not None:
            out.append(f"chart {reading.px_per_mm:.2f} px/mm")
        if reading.lp_mm is not None:
            out.append(f"{reading.lp_mm:.2f} line pairs/mm on the chart")
    return " · ".join(out)


class FocusWindow(QDialog):
    """Live view + sharpness meter. Talks to the worker with jobs only."""

    job_requested = Signal(str, object)

    def __init__(
        self, worker: CameraWorker, live_running: bool, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self._worker = worker
        self._live_running = live_running
        self._create_widgets()
        self._create_layouts()
        self._connect_signals()
        self._apply_styles()
        self._load_initial_state()

    # --- Widget Construction ---
    def _create_widgets(self) -> None:
        self.live_view = LiveViewWidget()
        self.panel_widget = QWidget()
        self.title_label = QLabel(TITLE_TEXT)
        self.readout_label = QLabel(WAITING_TEXT)
        self.planes_label = QLabel("")
        self.best_label = QLabel("")
        self.hint_label = QLabel("")
        self.warning_label = QLabel("")
        self.info_label = QLabel("")
        self.reset_button = QPushButton(RESET_TEXT)
        self.auto_check = QCheckBox(AUTO_TEXT)
        self.timer = QTimer(self)
        self._busy = False
        self._closed = False
        self._last: FocusReading | None = None

    # --- Layout ---
    def _create_layouts(self) -> None:
        self.main_layout = QHBoxLayout(self)
        self.panel_layout = QVBoxLayout(self.panel_widget)
        self.main_layout.setContentsMargins(MARGIN, MARGIN, MARGIN, MARGIN)
        self.main_layout.setSpacing(SPACING)
        self.main_layout.addWidget(self.live_view, 1)
        self.main_layout.addWidget(self.panel_widget, 0)
        self.panel_layout.setContentsMargins(0, 0, 0, 0)
        self.panel_layout.setSpacing(SPACING)
        for w in (
            self.title_label,
            self.readout_label,
            self.planes_label,
            self.best_label,
            self.hint_label,
            self.warning_label,
            self.info_label,
            self.auto_check,
            self.reset_button,
        ):
            self.panel_layout.addWidget(w)
        self.panel_layout.addStretch(1)

    # --- Signals & Slots ---
    def _connect_signals(self) -> None:
        self.job_requested.connect(self._worker.run_job)
        self._worker.frame_ready.connect(self._on_worker_frame_ready)
        self._worker.job_done.connect(self._on_worker_job_done)
        self._worker.job_failed.connect(self._on_worker_job_failed)
        self.reset_button.clicked.connect(self._on_reset_button_clicked)
        self.timer.timeout.connect(self._on_timer_timeout)

    # --- Styling ---
    def _apply_styles(self) -> None:
        self.setWindowTitle(WINDOW_TITLE)
        self.title_label.setObjectName("secondaryLabel")
        self.readout_label.setObjectName("focusReadout")
        self.best_label.setObjectName("focusBest")
        self.hint_label.setObjectName("focusHint")
        self.warning_label.setObjectName("focusWarning")
        self.info_label.setObjectName("secondaryLabel")

    # --- Initial State ---
    def _load_initial_state(self) -> None:
        self.resize(WINDOW_WIDTH, WINDOW_HEIGHT)
        self.panel_widget.setFixedWidth(PANEL_WIDTH)
        for label in (self.title_label, self.hint_label, self.warning_label, self.info_label):
            label.setWordWrap(True)
        self.auto_check.setChecked(True)
        self.timer.setSingleShot(True)
        self.timer.setInterval(READ_INTERVAL_MS)
        if not self._live_running:
            self.job_requested.emit("live_start", {})
        self._submit("focus_reset")

    # --- Public (tests) ---
    def last_reading(self) -> FocusReading | None:
        return self._last

    def readout_text(self) -> str:
        return self.readout_label.text()

    # --- Helpers ---
    def _submit(self, job: str, /, **kwargs: Any) -> None:
        self._busy = True
        self.job_requested.emit(job, kwargs)

    def _show(self, reading: FocusReading) -> None:
        self._last = reading
        self.readout_label.setText(
            f"{reading.main:.3f}" if reading.main is not None else NO_VALUE_TEXT
        )
        self.planes_label.setText(plane_text(reading) if len(reading.mtf50) > 1 else "")
        best = reading.best_main
        self.best_label.setText(f"best so far {best:.3f}" if best is not None else "")
        self.hint_label.setText(
            (MOVED_TEXT + ". " if reading.reset else "") + reading.hint.capitalize()
        )
        self.warning_label.setText("\n".join(f"⚠ {w}" for w in reading.warnings))
        self.info_label.setText(info_text(reading))

    # --- Slots ---
    def _on_timer_timeout(self) -> None:
        if not self._closed and not self._busy:
            self._submit("focus_reading", auto_exposure=self.auto_check.isChecked())

    def _on_reset_button_clicked(self) -> None:
        self.timer.stop()
        self._submit("focus_reset")

    def _on_worker_frame_ready(self, image: QImage, stats: dict[str, Any]) -> None:
        if self._closed:
            return
        overlay = stats.get("clip_overlay")
        self.live_view.show_frame(image, overlay if isinstance(overlay, QImage) else None)

    def _on_worker_job_done(self, name: str, payload: object) -> None:
        if self._closed or name not in FOCUS_JOBS:
            return
        self._busy = False
        if isinstance(payload, FocusToolOutcome) and payload.reading is not None:
            self._show(payload.reading)
        elif name == "focus_reset":
            self.best_label.setText("")
        self.timer.start()

    def _on_worker_job_failed(self, name: str, code: str, message: str) -> None:
        if self._closed or name not in FOCUS_JOBS:
            return
        self._busy = False
        self.warning_label.setText(f"⚠ {message}")
        self.timer.start()

    # ------------------------------------------------------------ closing
    def done(self, result: int) -> None:
        """Stop reading; the camera and live view stay as they are."""
        if not self._closed:
            self._closed = True
            self.timer.stop()
            for signal, slot in (
                (self._worker.frame_ready, self._on_worker_frame_ready),
                (self._worker.job_done, self._on_worker_job_done),
                (self._worker.job_failed, self._on_worker_job_failed),
            ):
                with suppress(RuntimeError, TypeError):
                    signal.disconnect(slot)
        super().done(result)
