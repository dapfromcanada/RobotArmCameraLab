"""The "Camera setup" window: live video + the gated setup steps (Phase 10 step 5).

All camera work runs on the :class:`CameraWorker` thread (``setup_*`` jobs). This
window only sends jobs and shows what comes back: it never calls the service.
Every step passes or stops; a STOP says what the human must fix, then Retry.
"""

from __future__ import annotations

from contextlib import suppress
from typing import Any

from PySide6.QtCore import QTimer, Signal
from PySide6.QtGui import QImage
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from camera_lab.two_d.gui.widgets.layout_utils import remove_all_widgets
from camera_lab.two_d.gui.widgets.live_view_widget import LiveViewWidget
from camera_lab.two_d.gui.workers import CameraWorker, FocusOutcome, SetupOutcome
from camera_lab.two_d.setup import STEPS, StepName, StepResult

MARGIN = 10
SPACING = 8
ROW_MARGIN = 6
STATUS_WIDTH = 70
NAME_WIDTH = 150
PANEL_WIDTH = 520
WINDOW_WIDTH = 1200
WINDOW_HEIGHT = 760
FOCUS_INTERVAL_MS = 1000
BUTTON_HEIGHT = 36

WINDOW_TITLE = "Camera setup"
STEP_LABELS: dict[StepName, str] = {
    "backup": "1. Backup",
    "light": "2. Light stable",
    "brightness": "3. Brightness",
    "focus": "4. Focus",
    "brightness_recheck": "5. Brightness re-check",
    "white_balance": "6. White balance",
    "save": "7. Save",
}
STATE_PENDING = "pending"
STATE_RUNNING = "running"
STATE_OK = "pass"
STATE_SKIPPED = "skipped"
STATE_STOP = "stop"
STATUS_TEXT = {
    STATE_PENDING: "pending",
    STATE_RUNNING: "running",
    STATE_OK: "pass",
    STATE_SKIPPED: "skipped",
    STATE_STOP: "STOP",
}
LEVEL_INFO = "info"
LEVEL_OK = "pass"
LEVEL_STOP = "stop"

START_TEXT = "Starting the setup session..."
READY_TEXT = "Press Run next step. Live video stays on while the steps run."
RUNNING_TEXT = "Running: {step}..."
FOCUS_HELP = (
    "Turn the focus ring on the lens until the MTF50 value is as high as it gets, "
    "lock the ring, then press 'Focus set and locked'. The resolution chart must be in view."
)
BACKUP_NOTE = "Backup taken before saving:"
NO_READING_TEXT = "-"
NO_CHART_TEXT = "no resolution chart in view"
ALL_DONE_TEXT = "All steps passed and saved."
SAVE_READY_TEXT = "All checks passed. Save to User Set 1 when you are ready."

CONFIRM_TITLE = "Save to camera memory?"
CONFIRM_TEXT = (
    "This writes the current settings into the camera's memory (User Set 1) "
    "and makes User Set 1 the set the camera boots with.\n\n"
    "The camera's previous settings were backed up first (step 1) and can be restored.\n\n"
    "Write to the camera now?"
)

SETUP_JOBS = frozenset(
    {
        "setup_create",
        "setup_step",
        "setup_focus_reading",
        "setup_finish_focus",
        "setup_save",
    }
)


class SetupWindow(QDialog):
    """Guided camera setup next to a live view. Talks to the worker with jobs only."""

    job_requested = Signal(str, object)

    def __init__(
        self,
        worker: CameraWorker,
        live_running: bool,
        focus_interval_ms: int = FOCUS_INTERVAL_MS,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._worker = worker
        self._live_running = live_running
        self._focus_interval_ms = focus_interval_ms
        self._create_widgets()
        self._create_layouts()
        self._connect_signals()
        self._apply_styles()
        self._load_initial_state()

    # --- Widget Construction ---
    def _create_widgets(self) -> None:
        self.live_view = LiveViewWidget()
        self.panel_widget = QWidget()
        self.steps_title_label = QLabel("Setup steps")
        self.steps_container = QWidget()
        self.message_label = QLabel(START_TEXT)
        self.focus_frame = QFrame()
        self.focus_help_label = QLabel(FOCUS_HELP)
        self.focus_current_title_label = QLabel("MTF50 now")
        self.focus_current_label = QLabel(NO_READING_TEXT)
        self.focus_best_title_label = QLabel("Best so far")
        self.focus_best_label = QLabel(NO_READING_TEXT)
        self.focus_locked_button = QPushButton("Focus set and locked")
        self.run_next_button = QPushButton("Run next step")
        self.retry_button = QPushButton("Retry")
        self.save_button = QPushButton("Save to User Set 1...")
        self.on_site_box = QCheckBox("Installed on site: boot from User Set 1")
        self.notes_label = QLabel("")
        self.focus_timer = QTimer(self)

        self._row_frames: dict[StepName, QFrame] = {}
        self._status_labels: dict[StepName, QLabel] = {}
        self._message_labels: dict[StepName, QLabel] = {}
        self._row_states: dict[StepName, str] = {}
        self._row_messages: dict[StepName, str] = {}
        self._results: dict[StepName, StepResult] = {}
        self._session_ready = False
        self._busy = True
        self._focus_mode = False
        self._focus_pending = False
        self._next_step: StepName | None = None
        self._failed_step: StepName | None = None
        self._closed = False

    # --- Layout ---
    def _create_layouts(self) -> None:
        self.main_layout = QHBoxLayout(self)
        self.panel_layout = QVBoxLayout(self.panel_widget)
        self.steps_layout = QVBoxLayout(self.steps_container)
        self.focus_layout = QVBoxLayout(self.focus_frame)
        self.focus_current_layout = QHBoxLayout()
        self.focus_best_layout = QHBoxLayout()
        self.button_layout = QHBoxLayout()

        self.main_layout.setContentsMargins(MARGIN, MARGIN, MARGIN, MARGIN)
        self.main_layout.setSpacing(SPACING)
        self.main_layout.addWidget(self.live_view, 1)
        self.main_layout.addWidget(self.panel_widget, 0)

        self.panel_layout.setContentsMargins(0, 0, 0, 0)
        self.panel_layout.setSpacing(SPACING)
        self.steps_layout.setContentsMargins(0, 0, 0, 0)
        self.steps_layout.setSpacing(SPACING)
        self.focus_current_layout.addWidget(self.focus_current_title_label)
        self.focus_current_layout.addStretch(1)
        self.focus_current_layout.addWidget(self.focus_current_label)
        self.focus_best_layout.addWidget(self.focus_best_title_label)
        self.focus_best_layout.addStretch(1)
        self.focus_best_layout.addWidget(self.focus_best_label)
        self.focus_layout.addWidget(self.focus_help_label)
        self.focus_layout.addLayout(self.focus_current_layout)
        self.focus_layout.addLayout(self.focus_best_layout)
        self.focus_layout.addWidget(self.focus_locked_button)
        self.button_layout.addWidget(self.run_next_button)
        self.button_layout.addWidget(self.retry_button)
        self.button_layout.addWidget(self.on_site_box)
        self.button_layout.addWidget(self.save_button)

        self.panel_layout.addWidget(self.steps_title_label)
        self.panel_layout.addWidget(self.steps_container)
        self.panel_layout.addWidget(self.message_label)
        self.panel_layout.addWidget(self.focus_frame)
        self.panel_layout.addLayout(self.button_layout)
        self.panel_layout.addWidget(self.notes_label)
        self.panel_layout.addStretch(1)

    # --- Signals & Slots ---
    def _connect_signals(self) -> None:
        self.job_requested.connect(self._worker.run_job)
        self._worker.frame_ready.connect(self._on_worker_frame_ready)
        self._worker.job_done.connect(self._on_worker_job_done)
        self._worker.job_failed.connect(self._on_worker_job_failed)
        self.run_next_button.clicked.connect(self._on_run_next_button_clicked)
        self.retry_button.clicked.connect(self._on_retry_button_clicked)
        self.save_button.clicked.connect(self._on_save_button_clicked)
        self.focus_locked_button.clicked.connect(self._on_focus_locked_button_clicked)
        self.focus_timer.timeout.connect(self._on_focus_timer_timeout)

    # --- Styling ---
    def _apply_styles(self) -> None:
        self.setWindowTitle(WINDOW_TITLE)
        self.steps_title_label.setObjectName("secondaryLabel")
        self.message_label.setObjectName("setupMessage")
        self.focus_current_label.setObjectName("focusReadout")
        self.focus_best_label.setObjectName("focusBest")
        self.focus_help_label.setObjectName("secondaryLabel")
        self.notes_label.setObjectName("secondaryLabel")
        self._set_property(self.message_label, "level", LEVEL_INFO)

    # --- Initial State ---
    def _load_initial_state(self) -> None:
        self.resize(WINDOW_WIDTH, WINDOW_HEIGHT)
        self.panel_widget.setFixedWidth(PANEL_WIDTH)
        self.message_label.setWordWrap(True)
        self.focus_help_label.setWordWrap(True)
        self.notes_label.setWordWrap(True)
        self.focus_frame.setVisible(False)
        self.focus_locked_button.setFixedHeight(BUTTON_HEIGHT)
        self.run_next_button.setFixedHeight(BUTTON_HEIGHT)
        self.retry_button.setFixedHeight(BUTTON_HEIGHT)
        self.save_button.setFixedHeight(BUTTON_HEIGHT)
        self.focus_timer.setInterval(self._focus_interval_ms)
        self._populate_step_list()
        self._refresh_ui_state()
        if not self._live_running:
            self.job_requested.emit("live_start", {})
        self.job_requested.emit("setup_create", {})

    # ------------------------------------------------------------ public
    @property
    def next_step(self) -> StepName | None:
        """The step that is due (None when all passed)."""
        return self._next_step

    @property
    def failed_step(self) -> StepName | None:
        """The step that stopped and is waiting for Retry."""
        return self._failed_step

    @property
    def focus_mode(self) -> bool:
        """True while the focus readout is live."""
        return self._focus_mode

    def row_states(self) -> dict[StepName, str]:
        """Status text of every step row."""
        return {s: STATUS_TEXT[self._row_states[s]] for s in self._row_frames}

    def row_message(self, step: StepName) -> str:
        """The message shown on a step row."""
        return self._row_messages.get(step, "")

    def message_text(self) -> str:
        """The text of the message panel."""
        return self.message_label.text()

    def step_row_count(self) -> int:
        """Number of step rows in the list."""
        return self.steps_layout.count()

    # ----------------------------------------------- step list (factory)
    def _create_step_widget(self, step: StepName) -> QFrame:
        """Factory: one row (name, status, message) for ``step``."""
        row = QFrame()
        row.setObjectName("setupRow")
        name_label = QLabel(STEP_LABELS[step])
        name_label.setFixedWidth(NAME_WIDTH)
        status_label = QLabel(STATUS_TEXT[STATE_PENDING])
        status_label.setObjectName("setupStatus")
        status_label.setFixedWidth(STATUS_WIDTH)
        message_label = QLabel("")
        message_label.setObjectName("secondaryLabel")
        message_label.setWordWrap(True)
        layout = QHBoxLayout(row)
        layout.setContentsMargins(ROW_MARGIN, ROW_MARGIN, ROW_MARGIN, ROW_MARGIN)
        layout.addWidget(name_label)
        layout.addWidget(status_label)
        layout.addWidget(message_label, 1)
        self._status_labels[step] = status_label
        self._message_labels[step] = message_label
        self._set_property(status_label, "state", STATE_PENDING)
        return row

    def _populate_step_list(self) -> None:
        """Clear and rebuild the seven step rows, all pending."""
        self._clear_step_list()
        for step in STEPS:
            row = self._create_step_widget(step)
            self._row_frames[step] = row
            self._row_states[step] = STATE_PENDING
            self.steps_layout.addWidget(row)

    def _clear_step_list(self) -> None:
        remove_all_widgets(self.steps_layout)
        self._row_frames.clear()
        self._status_labels.clear()
        self._message_labels.clear()
        self._row_states.clear()
        self._row_messages.clear()

    # ----------------------------------------------------------- helpers
    @staticmethod
    def _set_property(widget: QWidget, name: str, value: str) -> None:
        widget.setProperty(name, value)
        style = widget.style()
        style.unpolish(widget)
        style.polish(widget)

    def _set_row(self, step: StepName, state: str, message: str = "") -> None:
        self._row_states[step] = state
        self._row_messages[step] = message
        self._status_labels[step].setText(STATUS_TEXT[state])
        self._message_labels[step].setText(message)
        self._set_property(self._status_labels[step], "state", state)

    def _show_message(self, text: str, level: str) -> None:
        self.message_label.setText(text)
        self._set_property(self.message_label, "level", level)

    def _all_checks_passed(self) -> bool:
        return self._next_step == "save"

    def _refresh_ui_state(self) -> None:
        idle = self._session_ready and not self._busy
        due = self._next_step
        can_run = idle and due is not None and due != "save" and self._failed_step is None
        self.run_next_button.setEnabled(can_run and not self._focus_mode)
        self.retry_button.setEnabled(idle and self._failed_step is not None)
        self.save_button.setEnabled(idle and self._all_checks_passed())
        self.focus_locked_button.setEnabled(idle and self._focus_mode)
        self.focus_frame.setVisible(self._focus_mode)

    def _submit(self, job: str, /, **kwargs: Any) -> None:
        self.job_requested.emit(job, kwargs)

    def _run_step(self, step: StepName) -> None:
        """Start ``step``: focus is interactive, the others are one worker job."""
        self._failed_step = None
        self.notes_label.setText("")
        if step == "focus":
            self._enter_focus_mode()
            return
        self._busy = True
        self._set_row(step, STATE_RUNNING, "")
        self._show_message(RUNNING_TEXT.format(step=STEP_LABELS[step]), LEVEL_INFO)
        self._refresh_ui_state()
        self._submit("setup_step", step=step)

    def _enter_focus_mode(self) -> None:
        self._focus_mode = True
        self._focus_pending = False
        self._set_row("focus", STATE_RUNNING, "turn the focus ring, then lock it")
        self._show_message(FOCUS_HELP, LEVEL_INFO)
        self._refresh_ui_state()
        self.focus_timer.start()
        self._request_focus_reading()

    def _leave_focus_mode(self) -> None:
        self._focus_mode = False
        self.focus_timer.stop()

    def _request_focus_reading(self) -> None:
        if self._focus_pending or self._busy or not self._focus_mode:
            return
        self._focus_pending = True
        self._submit("setup_focus_reading")

    def _confirm_save(self) -> bool:
        """Ask before writing camera memory (tests replace this)."""
        answer = QMessageBox.question(
            self,
            CONFIRM_TITLE,
            CONFIRM_TEXT,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _show_step_result(self, outcome: SetupOutcome) -> None:
        result = outcome.result
        self._next_step = outcome.next_step
        if result is None:
            return
        self._results[result.step] = result
        if result.status == "fail":
            self._failed_step = result.step
            self._set_row(result.step, STATE_STOP, result.message)
            self._show_message(f"STOP - {result.message}", LEVEL_STOP)
            return
        self._failed_step = None
        state = STATE_SKIPPED if result.status == "skipped" else STATE_OK
        self._set_row(result.step, state, result.message)
        if result.step == "save":
            self._show_message(f"{ALL_DONE_TEXT} {result.message}", LEVEL_OK)
            self.notes_label.setText(self._save_notes_text(result))
        elif self._all_checks_passed():
            self._show_message(SAVE_READY_TEXT, LEVEL_OK)
        else:
            self._show_message(result.message, LEVEL_OK)

    @staticmethod
    def _save_notes_text(result: StepResult) -> str:
        """Backup id and the notes the camera reported (settings a user set cannot store...)."""
        lines: list[str] = []
        user_set = result.data.get("user_set", {})
        if user_set.get("backup_before"):
            lines.append(f"{BACKUP_NOTE} {user_set['backup_before']}")
        lines.extend(str(note) for note in result.data.get("notes", []))
        return "\n".join(lines)

    def _show_focus_reading(self, reading: FocusOutcome) -> None:
        if reading.mtf50_cyc_px is None:
            self.focus_current_label.setText(NO_READING_TEXT)
            self._set_row("focus", STATE_RUNNING, reading.message or NO_CHART_TEXT)
        else:
            lp = "" if reading.mtf50_lp_mm is None else f"  ({reading.mtf50_lp_mm:.1f} lp/mm)"
            self.focus_current_label.setText(f"{reading.mtf50_cyc_px:.3f} cyc/px{lp}")
            self._set_row("focus", STATE_RUNNING, "turn the focus ring, then lock it")
        if reading.best is None:
            self.focus_best_label.setText(NO_READING_TEXT)
        else:
            self.focus_best_label.setText(f"{reading.best:.3f} cyc/px")

    # ------------------------------------------------------------- slots
    def _on_worker_frame_ready(self, image: QImage, stats: dict[str, Any]) -> None:
        if self._closed:
            return
        overlay = stats.get("clip_overlay")
        self.live_view.show_frame(image, overlay if isinstance(overlay, QImage) else None)
        self.live_view.set_info(
            f"{stats.get('width')}x{stats.get('height')} {stats.get('pixel_format')}  "
            f"frame {stats.get('frame_id')}"
        )

    def _on_worker_job_done(self, name: str, payload: object) -> None:
        if self._closed or name not in SETUP_JOBS:
            return
        if name == "setup_focus_reading":
            self._focus_pending = False
            if isinstance(payload, FocusOutcome) and self._focus_mode:
                self._show_focus_reading(payload)
            return
        self._busy = False
        if isinstance(payload, SetupOutcome):
            if name == "setup_create":
                self._session_ready = True
                self._next_step = payload.next_step
                self._show_message(READY_TEXT, LEVEL_INFO)
            else:
                if name == "setup_finish_focus":
                    self._leave_focus_mode()
                self._show_step_result(payload)
        self._refresh_ui_state()

    def _on_worker_job_failed(self, name: str, code: str, message: str) -> None:
        if self._closed or name not in SETUP_JOBS:
            return
        if name == "setup_focus_reading":
            self._focus_pending = False
            self._show_message(f"STOP - {message}", LEVEL_STOP)
            return
        self._busy = False
        text = f"STOP - {message}"
        if name == "setup_create":
            self._show_message(text, LEVEL_STOP)
        else:
            step = self._failed_or_running_step()
            self._failed_step = step
            if step is not None:
                self._set_row(step, STATE_STOP, message)
            self._show_message(text, LEVEL_STOP)
        self._refresh_ui_state()

    def _failed_or_running_step(self) -> StepName | None:
        for step, state in self._row_states.items():
            if state == STATE_RUNNING:
                return step
        return self._next_step

    def _on_run_next_button_clicked(self) -> None:
        if self._next_step is not None and self._next_step != "save":
            self._run_step(self._next_step)

    def _on_retry_button_clicked(self) -> None:
        if self._failed_step is not None:
            self._run_step(self._failed_step)

    def _on_focus_locked_button_clicked(self) -> None:
        self._busy = True
        self.focus_timer.stop()
        self._refresh_ui_state()
        self._submit("setup_finish_focus", locked=True)

    def _on_focus_timer_timeout(self) -> None:
        self._request_focus_reading()

    def _on_save_button_clicked(self) -> None:
        if not self._confirm_save():
            self._show_message("Not saved: the camera memory was not changed.", LEVEL_INFO)
            return
        self._busy = True
        self._set_row("save", STATE_RUNNING, "")
        self._show_message(RUNNING_TEXT.format(step=STEP_LABELS["save"]), LEVEL_INFO)
        self._refresh_ui_state()
        self._submit("setup_save", confirm=True, on_site=self.on_site_box.isChecked())

    # ------------------------------------------------------------ closing
    def done(self, result: int) -> None:
        """Stop reacting to the worker; the camera and live view stay as they are."""
        if not self._closed:
            self._closed = True
            self.focus_timer.stop()
            for signal, slot in (
                (self._worker.frame_ready, self._on_worker_frame_ready),
                (self._worker.job_done, self._on_worker_job_done),
                (self._worker.job_failed, self._on_worker_job_failed),
            ):
                with suppress(RuntimeError, TypeError):
                    signal.disconnect(slot)
        super().done(result)
