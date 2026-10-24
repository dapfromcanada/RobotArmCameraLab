"""The "Camera fingerprint" window: live video + the gated fingerprint steps (Phase 11).

All camera work runs on the :class:`CameraWorker` thread (``fingerprint_*`` jobs).
This window only sends jobs and shows what comes back: it never calls the service.
Before each step it says what the human must do (lens cap, diffuser...); a step
that fails STOPS with the reason, the human fixes it, then Retry.
"""

from __future__ import annotations

from contextlib import suppress
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QGuiApplication, QImage
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from camera_lab.two_d.fingerprint.session import FP_SETUPS, FpResult, FpSetup, StepPlan
from camera_lab.two_d.gui.widgets.layout_utils import remove_all_widgets
from camera_lab.two_d.gui.widgets.live_view_widget import LiveViewWidget
from camera_lab.two_d.gui.workers import CameraWorker, FingerprintOutcome

MARGIN = 10
SPACING = 8
ROW_MARGIN = 6
STATUS_WIDTH = 70
NAME_WIDTH = 140
KEY_WIDTH = 190
PANEL_WIDTH = 560
RESULTS_MIN_HEIGHT = 160
WINDOW_WIDTH = 1300
WINDOW_HEIGHT = 900
BUTTON_HEIGHT = 36

WINDOW_TITLE = "Camera fingerprint"
#: Row labels until the session's plan arrives (the plan's labels name the light).
DEFAULT_LABELS: dict[FpSetup, str] = {
    "cap": "1. Lens cap on",
    "diffuser": "2. Diffuser on",
    "light_2": "3. Light change",
    "light_3": "4. Light change",
    "light_4": "5. Light change",
    "lens": "6. Lens / iris",
    "finish": "7. Finish",
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

START_TEXT = "Starting the fingerprint session..."
READY_TEXT = "Read the instruction, then press Run step. Live video stays on while steps run."
RUNNING_TEXT = (
    "Running: {step}... (minutes). camera_lab changes the exposure while it measures, so the "
    "live view brightness jumps: do not touch the lens or the light until the row says pass "
    "or STOP."
)
RETRY_HINT = " Fix it, tick the checklist again, then Retry."
ALL_DONE_TEXT = "Fingerprint complete."
CHECKS_TITLE = "Before Run step, confirm:"
COPY_TEXT = "Copy messages"
COPIED_TEXT = "Messages copied to the clipboard (paste them into an email or a chat)."
#: Messages and results can be selected with the mouse and copied (Derek 2026-10-22).
SELECTABLE = Qt.TextInteractionFlag.TextSelectableByMouse
LENS_LABEL = "Lens / iris:"
LENS_PLACEHOLDER = (
    "required: e.g. Kowa 8 mm, f/1.4 (fully open), dimmer 6 - recorded with each step"
)
DONE_INSTRUCTION = "All steps passed. The fingerprint is saved."
NO_VALUE_TEXT = "-"
RISES_TEXT = "SNR rises at"
TABLE_HEADER = ("gain dB", "exposure us", "signal DN", "sigma DN", "SNR")

FINGERPRINT_JOBS = frozenset({"fingerprint_create", "fingerprint_step", "fingerprint_skip"})
SKIP_TEXT = "Skip"
SKIP_PROMPT = "Why skip this setup? (recorded in the report)"


class FingerprintWindow(QDialog):
    """Guided camera fingerprint next to a live view. Talks to the worker with jobs only."""

    job_requested = Signal(str, object)

    def __init__(
        self,
        worker: CameraWorker,
        live_running: bool,
        parent: QWidget | None = None,
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
        self.instruction_label = QLabel("")
        self.lens_label = QLabel(LENS_LABEL)
        self.lens_edit = QLineEdit()
        self.steps_title_label = QLabel("Fingerprint steps")
        self.steps_container = QWidget()
        self.message_label = QLabel(START_TEXT)
        self.checks_title_label = QLabel(CHECKS_TITLE)
        self.checks_container = QWidget()
        self._check_boxes: list[QCheckBox] = []
        self._checks_step: FpSetup | None = None
        self._plan: dict[FpSetup, StepPlan] = {}
        self.run_step_button = QPushButton("Run step")
        self.retry_button = QPushButton("Retry")
        self.copy_button = QPushButton(COPY_TEXT)
        self.skip_button = QPushButton(SKIP_TEXT)
        self.results_title_label = QLabel("Results")
        self.results_scroll = QScrollArea()
        self.results_container = QWidget()
        self.folder_label = QLabel("")

        self._row_frames: dict[FpSetup, QFrame] = {}
        self._status_labels: dict[FpSetup, QLabel] = {}
        self._message_labels: dict[FpSetup, QLabel] = {}
        self._row_states: dict[FpSetup, str] = {}
        self._row_messages: dict[FpSetup, str] = {}
        self._results: dict[FpSetup, FpResult] = {}
        self._session_ready = False
        self._busy = True
        self._next_step: FpSetup | None = None
        self._failed_step: FpSetup | None = None
        self._closed = False

    # --- Layout ---
    def _create_layouts(self) -> None:
        self.main_layout = QHBoxLayout(self)
        self.panel_layout = QVBoxLayout(self.panel_widget)
        self.steps_layout = QVBoxLayout(self.steps_container)
        self.button_layout = QHBoxLayout()
        self.lens_layout = QHBoxLayout()
        self.results_layout = QVBoxLayout(self.results_container)
        self.checks_layout = QVBoxLayout(self.checks_container)
        self.checks_layout.setContentsMargins(0, 0, 0, 0)

        self.main_layout.setContentsMargins(MARGIN, MARGIN, MARGIN, MARGIN)
        self.main_layout.setSpacing(SPACING)
        self.main_layout.addWidget(self.live_view, 1)
        self.main_layout.addWidget(self.panel_widget, 0)

        self.panel_layout.setContentsMargins(0, 0, 0, 0)
        self.panel_layout.setSpacing(SPACING)
        self.steps_layout.setContentsMargins(0, 0, 0, 0)
        self.steps_layout.setSpacing(SPACING)
        self.results_layout.setContentsMargins(0, 0, 0, 0)
        self.results_layout.setSpacing(1)
        self.results_scroll.setWidget(self.results_container)
        self.button_layout.addWidget(self.run_step_button)
        self.button_layout.addWidget(self.retry_button)
        self.button_layout.addWidget(self.skip_button)
        self.button_layout.addWidget(self.copy_button)

        self.lens_layout.addWidget(self.lens_label)
        self.lens_layout.addWidget(self.lens_edit, 1)
        self.panel_layout.addWidget(self.instruction_label)
        self.panel_layout.addLayout(self.lens_layout)
        self.panel_layout.addWidget(self.checks_title_label)
        self.panel_layout.addWidget(self.checks_container)
        self.panel_layout.addWidget(self.steps_title_label)
        self.panel_layout.addWidget(self.steps_container)
        self.panel_layout.addWidget(self.message_label)
        self.panel_layout.addLayout(self.button_layout)
        self.panel_layout.addWidget(self.results_title_label)
        self.panel_layout.addWidget(self.results_scroll, 1)
        self.panel_layout.addWidget(self.folder_label)

    # --- Signals & Slots ---
    def _connect_signals(self) -> None:
        self.job_requested.connect(self._worker.run_job)
        self._worker.frame_ready.connect(self._on_worker_frame_ready)
        self._worker.job_done.connect(self._on_worker_job_done)
        self._worker.job_failed.connect(self._on_worker_job_failed)
        self.run_step_button.clicked.connect(self._on_run_step_button_clicked)
        self.retry_button.clicked.connect(self._on_retry_button_clicked)
        self.copy_button.clicked.connect(self._on_copy_button_clicked)
        self.skip_button.clicked.connect(self._on_skip_button_clicked)
        self.lens_edit.textChanged.connect(self._on_lens_edit_text_changed)

    # --- Styling ---
    def _apply_styles(self) -> None:
        self.setWindowTitle(WINDOW_TITLE)
        self.instruction_label.setObjectName("fpInstruction")
        self.steps_title_label.setObjectName("secondaryLabel")
        self.checks_title_label.setObjectName("secondaryLabel")
        self.results_title_label.setObjectName("secondaryLabel")
        self.message_label.setObjectName("setupMessage")
        self.folder_label.setObjectName("secondaryLabel")
        self._set_property(self.message_label, "level", LEVEL_INFO)

    # --- Initial State ---
    def _load_initial_state(self) -> None:
        self.resize(WINDOW_WIDTH, WINDOW_HEIGHT)
        self.panel_widget.setFixedWidth(PANEL_WIDTH)
        self.instruction_label.setWordWrap(True)
        self.message_label.setWordWrap(True)
        self.folder_label.setWordWrap(True)
        self.results_scroll.setWidgetResizable(True)
        self.results_scroll.setMinimumHeight(RESULTS_MIN_HEIGHT)
        self.lens_edit.setPlaceholderText(LENS_PLACEHOLDER)
        self.run_step_button.setFixedHeight(BUTTON_HEIGHT)
        self.retry_button.setFixedHeight(BUTTON_HEIGHT)
        self.copy_button.setFixedHeight(BUTTON_HEIGHT)
        self.skip_button.setFixedHeight(BUTTON_HEIGHT)
        for label in (self.message_label, self.instruction_label, self.folder_label):
            label.setTextInteractionFlags(SELECTABLE)
        self._populate_step_list()
        self._refresh_ui_state()
        if not self._live_running:
            self.job_requested.emit("live_start", {})
        self.job_requested.emit("fingerprint_create", {})

    # ------------------------------------------------------------ public
    @property
    def next_step(self) -> FpSetup | None:
        """The step that is due (None when all passed)."""
        return self._next_step

    @property
    def failed_step(self) -> FpSetup | None:
        """The step that stopped and is waiting for Retry."""
        return self._failed_step

    def row_states(self) -> dict[FpSetup, str]:
        """Status text of every step row."""
        return {s: STATUS_TEXT[self._row_states[s]] for s in self._row_frames}

    def row_message(self, step: FpSetup) -> str:
        """The message shown on a step row."""
        return self._row_messages.get(step, "")

    def message_text(self) -> str:
        """The text of the message panel."""
        return self.message_label.text()

    def instruction_text(self) -> str:
        """The instruction for the step that is due."""
        return self.instruction_label.text()

    def step_row_count(self) -> int:
        """Number of step rows in the list."""
        return self.steps_layout.count()

    def result_texts(self) -> list[str]:
        """The text of every result row ("key  value"), top to bottom."""
        out: list[str] = []
        for i in range(self.results_layout.count()):
            item = self.results_layout.itemAt(i)
            row = item.widget() if item is not None else None
            if row is not None:
                out.append("  ".join(lb.text() for lb in row.findChildren(QLabel)))
        return out

    def copy_text(self) -> str:
        """Every step's status and message, the current message and the results, as text."""
        lines = [WINDOW_TITLE, ""]
        for step in self._row_frames:
            state = STATUS_TEXT[self._row_states[step]]
            msg = self._row_messages.get(step, "")
            lines.append(f"{self._label(step)} [{state}]" + (f": {msg}" if msg else ""))
        lines += ["", f"Message: {self.message_label.text()}"]
        results = self.result_texts()
        if results:
            lines += ["", "Results:", *results]
        if self.folder_label.text():
            lines += ["", self.folder_label.text()]
        return "\n".join(lines)

    def result_row_count(self) -> int:
        """Number of rows in the results list."""
        return len(self.result_texts())

    # ----------------------------------------------- step list (factory)
    def _create_step_widget(self, step: FpSetup) -> QFrame:
        """Factory: one row (name, status, message) for ``step``."""
        row = QFrame()
        row.setObjectName("setupRow")
        name_label = QLabel(self._label(step))
        name_label.setWordWrap(True)
        name_label.setFixedWidth(NAME_WIDTH)
        status_label = QLabel(STATUS_TEXT[STATE_PENDING])
        status_label.setObjectName("setupStatus")
        status_label.setFixedWidth(STATUS_WIDTH)
        message_label = QLabel("")
        message_label.setObjectName("secondaryLabel")
        message_label.setWordWrap(True)
        message_label.setTextInteractionFlags(SELECTABLE)
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
        """Clear and rebuild the step rows: pending, or skipped where the plan does not
        use the step."""
        self._clear_step_list()
        for step in FP_SETUPS:
            plan = self._plan.get(step)
            if plan is not None and not plan.active:
                continue  # not used for this camera: no row
            row = self._create_step_widget(step)
            self._row_frames[step] = row
            self._row_states[step] = STATE_PENDING
            self.steps_layout.addWidget(row)

    # ----------------------------------------------------- checklist
    def _label(self, step: FpSetup) -> str:
        plan = self._plan.get(step)
        return plan.label if plan is not None else DEFAULT_LABELS[step]

    def _populate_checks(self, step: FpSetup | None) -> None:
        """One unticked box per checklist item of ``step`` (reset after every change)."""
        remove_all_widgets(self.checks_layout)
        self._check_boxes = []
        self._checks_step = step
        plan = self._plan.get(step) if step is not None else None
        for text in plan.checks if plan is not None else ():
            box = QCheckBox(text)
            box.toggled.connect(self._on_check_toggled)
            self.checks_layout.addWidget(box)
            self._check_boxes.append(box)
        self.checks_title_label.setVisible(bool(self._check_boxes))

    def _checks_done(self) -> bool:
        return all(box.isChecked() for box in self._check_boxes)

    def checklist(self) -> list[str]:
        """The checklist items shown now."""
        return [box.text() for box in self._check_boxes]

    def tick_all(self) -> None:
        """Tick every checklist item (as the human does)."""
        for box in self._check_boxes:
            box.setChecked(True)

    def _on_check_toggled(self, _checked: bool) -> None:
        self._refresh_ui_state()

    def _clear_step_list(self) -> None:
        remove_all_widgets(self.steps_layout)
        self._row_frames.clear()
        self._status_labels.clear()
        self._message_labels.clear()
        self._row_states.clear()
        self._row_messages.clear()

    # --------------------------------------------- results (factory)
    def _create_result_widget(self, cells: tuple[str, ...], is_table: bool = False) -> QFrame:
        """Factory: one results row. Two cells = key/value; more = a table row."""
        row = QFrame()
        row.setObjectName("fpResultRow")
        layout = QHBoxLayout(row)
        layout.setContentsMargins(ROW_MARGIN, 0, ROW_MARGIN, 0)
        for i, text in enumerate(cells):
            label = QLabel(text)
            label.setTextInteractionFlags(SELECTABLE)
            if not is_table:
                label.setObjectName("fpResultKey" if i == 0 else "fpResultValue")
            if not is_table and i == 0:
                label.setFixedWidth(KEY_WIDTH)
            layout.addWidget(label, 0 if i == 0 and not is_table else 1)
        return row

    def _populate_results(self, pairs: list[tuple[str, ...]], table: list[tuple[str, ...]]) -> None:
        """Clear and rebuild the results: key/value rows, then the noise-vs-gain table."""
        self._clear_results()
        for cells in pairs:
            self.results_layout.addWidget(self._create_result_widget(cells))
        for cells in table:
            self.results_layout.addWidget(self._create_result_widget(cells, is_table=True))
        self.results_layout.addStretch(1)

    def _clear_results(self) -> None:
        remove_all_widgets(self.results_layout)

    # ----------------------------------------------------------- helpers
    @staticmethod
    def _set_property(widget: QWidget, name: str, value: str) -> None:
        widget.setProperty(name, value)
        style = widget.style()
        style.unpolish(widget)
        style.polish(widget)

    def _set_row(self, step: FpSetup, state: str, message: str = "") -> None:
        self._row_states[step] = state
        self._row_messages[step] = message
        self._status_labels[step].setText(STATUS_TEXT[state])
        self._message_labels[step].setText(message)
        self._set_property(self._status_labels[step], "state", state)

    def _show_message(self, text: str, level: str) -> None:
        self.message_label.setText(text)
        self._set_property(self.message_label, "level", level)

    def _refresh_instruction(self) -> None:
        due = self._failed_step or self._next_step
        plan = self._plan.get(due) if due is not None else None
        text = DONE_INSTRUCTION if due is None else (plan.instruction if plan else "")
        self.instruction_label.setText(text)
        if due != self._checks_step:
            self._populate_checks(due)

    def _refresh_ui_state(self) -> None:
        idle = self._session_ready and not self._busy
        self._refresh_instruction()
        # the lens and iris go into every row's record (Derek 2026-10-24: an empty box left
        # the JAI report saying "not stated")
        ticked = self._checks_done() and bool(self.lens_edit.text().strip())
        can_run = idle and self._next_step is not None and self._failed_step is None
        self.run_step_button.setEnabled(can_run and ticked)
        self.retry_button.setEnabled(idle and self._failed_step is not None and ticked)
        failed = self._plan.get(self._failed_step) if self._failed_step is not None else None
        self.skip_button.setEnabled(idle and failed is not None and failed.can_skip)

    def _submit(self, job: str, /, **kwargs: Any) -> None:
        self.job_requested.emit(job, kwargs)

    def _run_step(self, step: FpSetup) -> None:
        self._failed_step = None
        self._busy = True
        self._set_row(step, STATE_RUNNING, "")
        confirmed = tuple(box.text() for box in self._check_boxes if box.isChecked())
        self._show_message(RUNNING_TEXT.format(step=self._label(step)), LEVEL_INFO)
        self._checks_step = None  # a new, unticked checklist after the step (strict)
        self._refresh_ui_state()
        self._submit("fingerprint_step", step=step, lens=self.lens_edit.text(), confirmed=confirmed)

    def _apply_outcome(self, outcome: FingerprintOutcome) -> None:
        """Plan, lens and the due step from the worker."""
        if outcome.plan is not None and outcome.plan != self._plan:
            self._plan = dict(outcome.plan)
            states = dict(self._row_states)
            messages = dict(self._row_messages)
            self._populate_step_list()
            for step, state in states.items():
                if state != STATE_PENDING:
                    self._set_row(step, state, messages.get(step, ""))
        self.lens_edit.setReadOnly(outcome.has_recipe)
        self.lens_edit.setEnabled(not outcome.has_recipe)
        if outcome.has_recipe or not self.lens_edit.text():
            self.lens_edit.setText(outcome.lens)
        self._next_step = outcome.next_step

    def _show_step_result(self, outcome: FingerprintOutcome) -> None:
        result = outcome.result
        self._apply_outcome(outcome)
        if result is None or result.step not in self._row_frames:
            return
        step = result.step
        self._results[step] = result
        if result.status == "fail":
            self._failed_step = step
            self._checks_step = None  # re-confirm after a STOP
            self._set_row(step, STATE_STOP, result.message)
            hint = " Or Skip (recorded in the report)." if self._plan[step].can_skip else ""
            self._show_message(f"STOP - {result.message}.{RETRY_HINT}{hint}", LEVEL_STOP)
            return
        self._failed_step = None
        state = STATE_SKIPPED if result.status == "skipped" else STATE_OK
        self._set_row(step, state, result.message)
        if result.step == "finish" and result.status == "pass":
            self._show_message(f"{ALL_DONE_TEXT} {result.message}", LEVEL_OK)
            self._show_report(result)
        else:
            self._show_message(result.message, LEVEL_OK)

    def _result_pairs(self) -> list[tuple[str, ...]]:
        """Key results of the finished steps as (name, value) rows."""
        data = self._measurements()
        ptc = data.get("flat", {}).get("photon_transfer", {})
        flat = data.get("flat", {}).get("flat", {})
        dark = data.get("dark", {})

        def fmt(value: Any, spec: str, unit: str) -> str:
            return NO_VALUE_TEXT if value is None else f"{format(value, spec)} {unit}".strip()

        resp = data.get("flat", {}).get("response", {})
        snr = [v for _, v in resp.get("snr", [])]
        pairs: list[tuple[str, ...]] = []
        if ptc:
            pairs += [
                ("System gain", fmt(ptc.get("system_gain_dn_per_e"), ".4f", "DN/e-")),
                ("Read noise", fmt(ptc.get("read_noise_e"), ".1f", "e-")),
                ("Full well", fmt(ptc.get("full_well_e"), ".0f", "e-")),
            ]
        channels: dict[str, Any] = {}
        for key, value in data.items():
            if key.startswith("channels_"):
                channels.update(value.get("channels", {}))
        for name, ch in channels.items():
            cp = ch.get("photon_transfer", {})
            pairs.append(
                (
                    name,
                    f"{fmt(cp.get('system_gain_dn_per_e'), '.4f', 'DN/e-')}, read noise "
                    f"{fmt(cp.get('read_noise_e'), '.1f', 'e-')}, full well "
                    f"{fmt(cp.get('full_well_e'), '.0f', 'e-')}",
                )
            )
        pairs += [
            ("Linearity error", fmt(resp.get("linearity_error_pct"), ".2f", "%")),
            ("Peak SNR", fmt(max(snr) if snr else None, ".0f", "")),
            ("Dark current", fmt(dark.get("dark_current_dn_s"), ".1f", "DN/s")),
            ("Hot pixels", fmt(dark.get("hot_pixel_count"), "d", "")),
            ("PRNU", fmt(flat.get("prnu_pct"), ".2f", "%")),
            ("Vignetting (corner/centre)", fmt(flat.get("vignetting_corner_ratio"), ".2f", "")),
        ]
        colour = data.get("colour", {})
        for name, rel in colour.get("relative_to_green", {}).items():
            if not name.startswith("G"):
                pairs.append((f"{name} relative to green", fmt(rel, ".3f", "")))
        if colour.get("green_imbalance_pct") is not None:
            pairs.append(
                ("Green imbalance (Gr-Gb)", fmt(colour["green_imbalance_pct"], "+.2f", "%"))
            )
        rises = data.get("noise_gain", {}).get("snr_rises_at_db", [])
        if rises:
            at = ", ".join(f"{g:g} dB" for g in rises)
            pairs.append((RISES_TEXT, f"{at} (possible gain-mode switch)"))
        return pairs

    def _measurements(self) -> dict[str, dict[str, Any]]:
        """Each measurement's data, from the setup results (a setup's data is keyed by
        the measurements it made)."""
        out: dict[str, dict[str, Any]] = {}
        for res in self._results.values():
            for key, value in res.data.items():
                if isinstance(value, dict):
                    out[key] = value
        return out

    def _result_table(self) -> list[tuple[str, ...]]:
        table = self._measurements().get("noise_gain", {}).get("table", [])
        out: list[tuple[str, ...]] = [TABLE_HEADER] if table else []
        for r in table:
            out.append(
                (
                    f"{r['gain_db']:g}",
                    f"{r['exposure_us']:g}",
                    f"{r['signal_dn']:g}",
                    f"{r['sigma_dn']:g}",
                    NO_VALUE_TEXT if r["snr"] is None else f"{r['snr']:g}",
                )
            )
        return out

    def _show_report(self, result: FpResult) -> None:
        self._populate_results(self._result_pairs(), self._result_table())
        folder = result.data.get("report", {}).get("folder", NO_VALUE_TEXT)
        self.folder_label.setText(f"Folder: {folder}")

    # ------------------------------------------------------------- slots
    def skip_failed(self, reason: str) -> None:
        """Skip the failed setup (only where the plan allows it), with a reason."""
        step = self._failed_step
        if step is None or not self._plan[step].can_skip:
            return
        self._busy = True
        self._failed_step = None
        self._set_row(step, STATE_RUNNING, "")
        self._checks_step = None
        self._refresh_ui_state()
        self._submit("fingerprint_skip", step=step, reason=reason)

    def _on_skip_button_clicked(self) -> None:
        reason, ok = QInputDialog.getText(self, SKIP_TEXT, SKIP_PROMPT)
        if ok:
            self.skip_failed(reason)

    def _on_copy_button_clicked(self) -> None:
        QGuiApplication.clipboard().setText(self.copy_text())
        self._show_message(COPIED_TEXT, LEVEL_INFO)

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
        if self._closed or name not in FINGERPRINT_JOBS:
            return
        self._busy = False
        if isinstance(payload, FingerprintOutcome):
            if name == "fingerprint_create":
                self._session_ready = True
                self._apply_outcome(payload)
                self._show_message(READY_TEXT, LEVEL_INFO)
            else:
                self._show_step_result(payload)
        self._refresh_ui_state()

    def _on_worker_job_failed(self, name: str, code: str, message: str) -> None:
        if self._closed or name not in FINGERPRINT_JOBS:
            return
        self._busy = False
        if name != "fingerprint_create":
            step = self._running_step() or self._next_step
            self._failed_step = step
            self._checks_step = None  # re-confirm after a STOP
            if step is not None:
                self._set_row(step, STATE_STOP, message)
        self._show_message(f"STOP - {message}", LEVEL_STOP)
        self._refresh_ui_state()

    def _running_step(self) -> FpSetup | None:
        for step, state in self._row_states.items():
            if state == STATE_RUNNING:
                return step
        return None

    def _on_lens_edit_text_changed(self, _text: str) -> None:
        self._refresh_ui_state()

    def _on_run_step_button_clicked(self) -> None:
        if self._next_step is not None:
            self._run_step(self._next_step)

    def _on_retry_button_clicked(self) -> None:
        if self._failed_step is not None:
            self._run_step(self._failed_step)

    # ------------------------------------------------------------ closing
    def done(self, result: int) -> None:
        """Stop reacting to the worker; the camera and live view stay as they are."""
        if not self._closed:
            self._closed = True
            for signal, slot in (
                (self._worker.frame_ready, self._on_worker_frame_ready),
                (self._worker.job_done, self._on_worker_job_done),
                (self._worker.job_failed, self._on_worker_job_failed),
            ):
                with suppress(RuntimeError, TypeError):
                    signal.disconnect(slot)
        super().done(result)
