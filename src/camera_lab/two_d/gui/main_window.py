"""The main window: camera list | live view + histogram | Camera/Light/Scores/Runs tabs.

All camera and service work goes through :class:`CameraWorker` on its own
``QThread`` (queued signals). This class only reacts to its results.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from PySide6.QtCore import QRect, Qt, QThread, Signal
from PySide6.QtGui import QAction, QCloseEvent, QImage
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QSplitter,
    QStatusBar,
    QTabWidget,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from camera_lab.core.logging import get_logger
from camera_lab.two_d.catalog import PartArea
from camera_lab.two_d.gui import styles
from camera_lab.two_d.gui.constants import ACTOR
from camera_lab.two_d.gui.heartbeat import UiHeartbeat
from camera_lab.two_d.gui.widgets.camera_list_panel import CameraListPanel
from camera_lab.two_d.gui.widgets.camera_settings_panel import CameraSettingsPanel
from camera_lab.two_d.gui.widgets.fingerprint_window import FingerprintWindow
from camera_lab.two_d.gui.widgets.focus_window import FocusWindow
from camera_lab.two_d.gui.widgets.histogram_widget import HistogramWidget
from camera_lab.two_d.gui.widgets.light_panel import LightPanel
from camera_lab.two_d.gui.widgets.live_view_widget import LiveViewWidget
from camera_lab.two_d.gui.widgets.preset_dialog import PresetDialog
from camera_lab.two_d.gui.widgets.run_browser_panel import RunBrowserPanel
from camera_lab.two_d.gui.widgets.score_panel import ScorePanel
from camera_lab.two_d.gui.widgets.setup_window import SetupWindow
from camera_lab.two_d.gui.workers import (
    ApplyOutcome,
    CameraWorker,
    CaptureOutcome,
    LightOutcome,
    OpenOutcome,
    PresetListOutcome,
    PresetOutcome,
    ResetOutcome,
    RunDetailOutcome,
    RunsOutcome,
    TuneOutcome,
)
from camera_lab.two_d.models import ApplyResult, CameraInfo
from camera_lab.two_d.service import SettingsView, TwoDService

_log = get_logger(__name__)

WINDOW_TITLE = "camera_lab"
WINDOW_WIDTH = 1500
WINDOW_HEIGHT = 900
LEFT_WIDTH = 250
RIGHT_WIDTH = 460
CENTRE_MIN_WIDTH = 480
MARGIN = 0
THREAD_WAIT_MS = 10_000
HEAVY_JOBS = frozenset({"capture", "tune", "reset"})
LOCKED_BY = "locked by "
NO_LOCK_TEXT = "lock: -"
TAB_CAMERA = "Camera"
TAB_LIGHT = "Light"
TAB_SCORES = "Scores"
TAB_RUNS = "Runs"
SAVE_NEEDS_CAPTURE = "Save preset: capture an image first (or select a capture in Runs)"
MAX_CAPTURE_CHOICES = 50


class MainWindow(QMainWindow):
    """Bench GUI main window. Pass the service; it is used only by the worker thread."""

    job_requested = Signal(str, object)
    shutdown_requested = Signal()

    def __init__(self, service: TwoDService, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.service = service  # data only: handed to the worker, never called from here
        self._create_widgets()
        self._create_layouts()
        self._connect_signals()
        self._apply_styles()
        self._load_initial_state()

    # --- Widget Construction ---
    def _create_widgets(self) -> None:
        self.camera_list_panel = CameraListPanel()
        self.live_view = LiveViewWidget()
        self.histogram = HistogramWidget()
        self.camera_settings_panel = CameraSettingsPanel()
        self.light_panel = LightPanel()
        self.score_panel = ScorePanel()
        self.run_browser_panel = RunBrowserPanel()
        self.centre_widget = QWidget()
        self.right_tabs = QTabWidget()
        self.main_splitter = QSplitter(Qt.Orientation.Horizontal)

        self.toolbar = QToolBar("Main")
        self.live_action = QAction("Start live", self)
        self.capture_action = QAction("Capture", self)
        self.tune_action = QAction("Run classic tune", self)
        self.save_preset_action = QAction("Save preset...", self)
        self.apply_preset_action = QAction("Apply preset...", self)
        self.reset_action = QAction("Load camera defaults...", self)
        self.lights_off_action = QAction("All lights off", self)
        self.setup_action = QAction("Camera setup...", self)
        self.fingerprint_action = QAction("Camera fingerprint...", self)
        self.focus_action = QAction("Focus...", self)

        self.status_bar = QStatusBar()
        self.state_label = QLabel("")
        self.fps_label = QLabel("")
        self.lock_label = QLabel("")
        self.warning_label = QLabel("")

        self.worker_thread = QThread(self)
        self.worker = CameraWorker(self.service)
        self.heartbeat = UiHeartbeat(self)

        self._camera_id: str | None = None
        self._live = False
        self._busy_jobs: set[str] = set()
        self._capture_choices: list[tuple[str, str]] = []
        self._closed = False
        self._setup_window: SetupWindow | None = None
        self._fingerprint_window: FingerprintWindow | None = None
        self._focus_window: FocusWindow | None = None
        self._lock_note = ""  # who holds a camera we could not open
        self._job_handlers: dict[str, Callable[[Any], None]] = {
            "discover": self._show_discovered,
            "open": self._show_opened,
            "close": self._show_closed,
            "settings": self._show_settings,
            "apply": self._show_applied,
            "set_mode": self._show_applied,
            "reset": self._show_reset,
            "capture": self._show_captured,
            "tune": self._show_tuned,
            "lights_list": self._show_lights,
            "lights_set": self._show_lights,
            "lights_off": self._show_lights,
            "runs": self._show_runs,
            "run_detail": self._show_run_detail,
            "presets_list": self._show_preset_choices,
            "preset_save": self._show_preset_saved,
            "preset_apply": self._show_preset_applied,
            "part_area": self._show_part_area,
            "part_area_set": self._show_part_area_saved,
            "part_area_clear": self._show_part_area_cleared,
        }

    # --- Layout ---
    def _create_layouts(self) -> None:
        self.centre_layout = QVBoxLayout(self.centre_widget)
        self.centre_layout.setContentsMargins(MARGIN, MARGIN, MARGIN, MARGIN)
        self.centre_layout.addWidget(self.live_view, 1)
        self.centre_layout.addWidget(self.histogram)

        self.right_tabs.addTab(self.camera_settings_panel, TAB_CAMERA)
        self.right_tabs.addTab(self.light_panel, TAB_LIGHT)
        self.right_tabs.addTab(self.score_panel, TAB_SCORES)
        self.right_tabs.addTab(self.run_browser_panel, TAB_RUNS)

        self.main_splitter.addWidget(self.camera_list_panel)
        self.main_splitter.addWidget(self.centre_widget)
        self.main_splitter.addWidget(self.right_tabs)
        self.main_splitter.setStretchFactor(0, 0)
        self.main_splitter.setStretchFactor(1, 1)
        self.main_splitter.setStretchFactor(2, 0)
        self.main_splitter.setSizes(
            [LEFT_WIDTH, WINDOW_WIDTH - LEFT_WIDTH - RIGHT_WIDTH, RIGHT_WIDTH]
        )
        self.centre_widget.setMinimumWidth(CENTRE_MIN_WIDTH)
        self.setCentralWidget(self.main_splitter)

        for action in (self.live_action, self.capture_action, self.tune_action):
            self.toolbar.addAction(action)
        self.toolbar.addSeparator()
        for action in (self.save_preset_action, self.apply_preset_action, self.reset_action):
            self.toolbar.addAction(action)
        self.toolbar.addSeparator()
        self.toolbar.addAction(self.lights_off_action)
        self.toolbar.addSeparator()
        self.toolbar.addAction(self.setup_action)
        self.toolbar.addAction(self.fingerprint_action)
        self.toolbar.addAction(self.focus_action)
        self.addToolBar(self.toolbar)

        for label in (self.state_label, self.fps_label, self.lock_label):
            self.status_bar.addPermanentWidget(label)
        self.status_bar.addWidget(self.warning_label, 1)
        self.setStatusBar(self.status_bar)

    # --- Signals & Slots ---
    def _connect_signals(self) -> None:
        self.job_requested.connect(self.worker.run_job)
        self.shutdown_requested.connect(
            self.worker.shutdown, Qt.ConnectionType.BlockingQueuedConnection
        )
        self.worker.job_done.connect(self._on_worker_job_done)
        self.worker.job_failed.connect(self._on_worker_job_failed)
        self.worker.frame_ready.connect(self._on_worker_frame_ready)
        self.worker.live_state_changed.connect(self._on_worker_live_state_changed)
        self.worker.live_error.connect(self._on_worker_live_error)

        self.camera_list_panel.rescan_requested.connect(self._on_camera_list_panel_rescan_requested)
        self.camera_list_panel.camera_selected.connect(self._on_camera_list_panel_camera_selected)
        self.camera_list_panel.disconnect_requested.connect(
            self._on_camera_list_panel_disconnect_requested
        )
        self.camera_settings_panel.patch_requested.connect(
            self._on_camera_settings_panel_patch_requested
        )
        self.camera_settings_panel.mode_requested.connect(
            self._on_camera_settings_panel_mode_requested
        )
        self.light_panel.levels_requested.connect(self._on_light_panel_levels_requested)
        self.run_browser_panel.refresh_requested.connect(
            self._on_run_browser_panel_refresh_requested
        )
        self.run_browser_panel.run_selected.connect(self._on_run_browser_panel_run_selected)

        self.live_view.part_area_save_requested.connect(self._on_live_view_part_area_save_requested)
        self.live_view.part_area_clear_requested.connect(
            self._on_live_view_part_area_clear_requested
        )
        self.live_action.toggled.connect(self._on_live_action_toggled)
        self.capture_action.triggered.connect(self._on_capture_action_triggered)
        self.tune_action.triggered.connect(self._on_tune_action_triggered)
        self.save_preset_action.triggered.connect(self._on_save_preset_action_triggered)
        self.apply_preset_action.triggered.connect(self._on_apply_preset_action_triggered)
        self.reset_action.triggered.connect(self._on_reset_action_triggered)
        self.lights_off_action.triggered.connect(self._on_lights_off_action_triggered)
        self.setup_action.triggered.connect(self._on_setup_action_triggered)
        self.fingerprint_action.triggered.connect(self._on_fingerprint_action_triggered)
        self.focus_action.triggered.connect(self._on_focus_action_triggered)

    # --- Styling ---
    def _apply_styles(self) -> None:
        app = QApplication.instance()
        if isinstance(app, QApplication):
            styles.apply_dark_theme(app)
        self.setWindowTitle(WINDOW_TITLE)
        self.warning_label.setObjectName("warningLabel")
        for label in (self.state_label, self.fps_label, self.lock_label):
            label.setObjectName("secondaryLabel")

    # --- Initial State ---
    def _load_initial_state(self) -> None:
        self.resize(WINDOW_WIDTH, WINDOW_HEIGHT)
        self.live_action.setCheckable(True)
        self.worker.moveToThread(self.worker_thread)
        self.worker_thread.start()
        self.heartbeat.start()
        self._refresh_ui_state()
        self._submit("discover")

    # ------------------------------------------------------------ public
    @property
    def max_ui_stall_ms(self) -> float:
        """The longest UI stall measured by the heartbeat (milliseconds)."""
        return self.heartbeat.max_stall_ms

    @property
    def measured_live_fps(self) -> float:
        """The live preview rate measured by the worker."""
        return self.worker.measured_fps

    @property
    def setup_window(self) -> SetupWindow | None:
        """The open "Camera setup" window, if any."""
        return self._setup_window

    @property
    def fingerprint_window(self) -> FingerprintWindow | None:
        """The open "Camera fingerprint" window, if any."""
        return self._fingerprint_window

    @property
    def focus_window(self) -> FocusWindow | None:
        """The open "Focus" window, if any."""
        return self._focus_window

    @property
    def camera_id(self) -> str | None:
        """The open camera's serial."""
        return self._camera_id

    # ----------------------------------------------------------- helpers
    def _submit(self, job: str, /, **kwargs: Any) -> None:
        """Queue a job on the worker thread."""
        if job in HEAVY_JOBS:
            self._busy_jobs.add(job)
            self._refresh_ui_state()
        self.job_requested.emit(job, kwargs)

    def _show_warning(self, text: str) -> None:
        self.warning_label.setText(text)
        self.warning_label.setToolTip(text)

    def _refresh_ui_state(self) -> None:
        has_camera = self._camera_id is not None
        idle = not self._busy_jobs
        self.live_action.setEnabled(has_camera)
        self.capture_action.setEnabled(has_camera and idle)
        self.tune_action.setEnabled(has_camera and idle)
        self.save_preset_action.setEnabled(has_camera)
        self.apply_preset_action.setEnabled(has_camera)
        # one procedure on the camera at a time: setup, fingerprint or focus
        windows = (self._setup_window, self._fingerprint_window, self._focus_window)
        free = all(w is None for w in windows)
        self.setup_action.setEnabled(
            has_camera and idle and (free or self._setup_window is not None)
        )
        self.fingerprint_action.setEnabled(
            has_camera and idle and (free or self._fingerprint_window is not None)
        )
        self.focus_action.setEnabled(
            has_camera and idle and (free or self._focus_window is not None)
        )
        self.camera_list_panel.set_disconnect_enabled(has_camera and idle and free)
        self.reset_action.setEnabled(has_camera and idle and free)
        self.live_view.set_part_area_enabled(has_camera and idle)
        if not has_camera:
            state = "no camera"
        elif self._busy_jobs:
            state = f"{self._camera_id}: working ({', '.join(sorted(self._busy_jobs))})"
        else:
            state = f"{self._camera_id}: {'live' if self._live else 'open'}"
        self.state_label.setText(state)
        if has_camera:
            self.lock_label.setText(f"lock: gui ({self._camera_id})")
        else:
            self.lock_label.setText(self._lock_note or NO_LOCK_TEXT)
        self.live_action.setText("Stop live" if self._live else "Start live")

    def _close_setup_window(self) -> None:
        window, self._setup_window = self._setup_window, None
        if window is not None:
            window.close()

    def _close_fingerprint_window(self) -> None:
        window, self._fingerprint_window = self._fingerprint_window, None
        if window is not None:
            window.close()

    def _close_focus_window(self) -> None:
        window, self._focus_window = self._focus_window, None
        if window is not None:
            window.close()

    def _reset_camera_state(self) -> None:
        self._close_setup_window()
        self._close_fingerprint_window()
        self._close_focus_window()
        self._camera_id = None
        self._live = False
        self._busy_jobs.clear()
        self.live_action.blockSignals(True)
        self.live_action.setChecked(False)
        self.live_action.blockSignals(False)
        self.camera_list_panel.set_open_camera(None)
        self.camera_settings_panel.clear_settings()
        self.live_view.clear_frame()
        self.live_view.set_part_area(None)
        self.histogram.clear_histogram()
        self.score_panel.clear_scores()
        self.score_panel.clear_processors()
        self.fps_label.setText("")
        self._refresh_ui_state()

    def _show_apply_outcome(self, result: ApplyResult | None, view: SettingsView) -> None:
        self.camera_settings_panel.show_view(view)
        if result is not None:
            self.camera_settings_panel.apply_result(result)
            if result.warnings:
                self._show_warning("; ".join(result.warnings))

    def _exec_dialog(self, dialog: PresetDialog) -> bool:
        """Run a modal dialog (tests replace this)."""
        return dialog.exec() == QDialog.DialogCode.Accepted

    def _ask_preset(self, mode: str, choices: Sequence[tuple[str, str]]) -> PresetDialog | None:
        dialog = PresetDialog("save" if mode == "save" else "apply", choices, self)
        return dialog if self._exec_dialog(dialog) else None

    # ------------------------------------------------- job result display
    def _show_discovered(self, cameras: list[CameraInfo]) -> None:
        self.camera_list_panel.populate_cameras(cameras)
        self.camera_list_panel.set_open_camera(self._camera_id)
        self.status_bar.showMessage(f"{len(cameras)} camera(s) found")

    def _show_opened(self, outcome: OpenOutcome) -> None:
        self._camera_id = outcome.info.serial
        self._lock_note = ""
        self._live = False
        self.camera_list_panel.set_open_camera(self._camera_id)
        self.camera_settings_panel.populate_settings(outcome.view)
        self.camera_settings_panel.populate_modes(outcome.modes, outcome.mode, outcome.default_mode)
        self.live_view.clear_frame()
        self.histogram.clear_histogram()
        self.score_panel.clear_scores()
        self.score_panel.clear_processors()
        self._capture_choices.clear()
        self._show_warning("")
        self._refresh_ui_state()
        self.live_view.set_part_area(None)
        self._submit("lights_list")
        self._submit("runs", camera_id=self._camera_id)
        self._submit("part_area")

    def _show_closed(self, _payload: object) -> None:
        self._reset_camera_state()

    def _show_part_area(self, area: PartArea | None) -> None:
        self.live_view.set_part_area(
            None if area is None else QRect(area.x, area.y, area.width, area.height)
        )

    def _show_part_area_saved(self, area: PartArea | None) -> None:
        self._show_part_area(area)
        self.status_bar.showMessage("part area saved: scores now judge the part")

    def _show_part_area_cleared(self, _payload: object) -> None:
        self._show_part_area(None)
        self.status_bar.showMessage("part area cleared: the whole image is scored")

    def _confirm(self, title: str, text: str) -> bool:
        """Ask yes / no (tests replace this)."""
        answer = QMessageBox.question(self, title, text)
        return answer == QMessageBox.StandardButton.Yes

    def _show_settings(self, view: SettingsView) -> None:
        self.camera_settings_panel.show_view(view)

    def _show_applied(self, outcome: ApplyOutcome) -> None:
        self._show_apply_outcome(outcome.result, outcome.view)
        self.camera_settings_panel.show_mode(outcome.mode)

    def _show_reset(self, outcome: ResetOutcome) -> None:
        self._busy_jobs.discard("reset")
        self.camera_settings_panel.show_view(outcome.view)
        self.camera_settings_panel.show_mode(outcome.mode)
        res = outcome.result
        mismatches = res.get("mismatches") or []
        if mismatches:
            self._show_warning(
                f"Camera defaults loaded ({res.get('mode')}), but {len(mismatches)} setting(s) "
                f"did not take: {'; '.join(str(m) for m in mismatches)}"
            )
        else:
            self._show_warning("")
            self.status_bar.showMessage(
                f"camera defaults loaded: factory Default + {res.get('mode')}, every setting "
                f"read back (backup {res.get('backup_before')})"
            )
        self._refresh_ui_state()

    def _show_captured(self, outcome: CaptureOutcome) -> None:
        self._busy_jobs.discard("capture")
        for cap in outcome.captures:
            self.score_panel.populate_scores(cap.scores)
            label = f"{cap.capture_id}  (score {cap.scores.composite:.1f})"
            self._capture_choices.insert(0, (cap.capture_id, label))
        del self._capture_choices[MAX_CAPTURE_CHOICES:]
        self.score_panel.populate_processors(outcome.processors)
        self._refresh_ui_state()
        self.status_bar.showMessage(f"captured {outcome.captures[-1].capture_id}")
        self._submit("runs", camera_id=self._camera_id)

    def _show_tuned(self, outcome: TuneOutcome) -> None:
        self._busy_jobs.discard("tune")
        self.camera_settings_panel.show_view(outcome.view)
        self.camera_settings_panel.show_mode(outcome.mode)
        res = outcome.result
        self._show_warning(f"Tune {res.stop_reason}: {res.message}")
        if res.best_capture_id is not None:
            score = "" if res.best_composite is None else f" (score {res.best_composite:.1f})"
            self._capture_choices.insert(0, (res.best_capture_id, f"{res.best_capture_id}{score}"))
        self._refresh_ui_state()
        self._submit("lights_list")
        self._submit("runs", camera_id=self._camera_id)

    def _show_lights(self, outcome: LightOutcome) -> None:
        if self.light_panel.channel_ids() == [str(c["id"]) for c in outcome.channels]:
            self.light_panel.update_levels(outcome.channels, outcome.rejected)
        else:
            self.light_panel.populate_lights(outcome.channels)
            self.light_panel.update_levels(outcome.channels, outcome.rejected)

    def _show_runs(self, outcome: RunsOutcome) -> None:
        self.run_browser_panel.populate_runs(outcome.runs)

    def _show_run_detail(self, outcome: RunDetailOutcome) -> None:
        self.run_browser_panel.populate_captures(outcome.captures)

    def _show_preset_choices(self, outcome: PresetListOutcome) -> None:
        choices = [(p.name, p.name) for p in outcome.presets]
        if not choices:
            self._show_warning("No presets saved for this camera")
            return
        dialog = self._ask_preset("apply", choices)
        if dialog is not None:
            self._submit("preset_apply", name=dialog.choice_value())

    def _show_preset_saved(self, path: str) -> None:
        self._show_warning(f"Preset saved: {path}")

    def _show_preset_applied(self, outcome: PresetOutcome) -> None:
        self._show_apply_outcome(outcome.applied.apply, outcome.view)
        if outcome.channels:
            self._show_lights(LightOutcome(outcome.channels))

    # ---------------------------------------------------------- slots
    def _on_worker_job_done(self, name: str, payload: object) -> None:
        if self._closed:
            return
        handler = self._job_handlers.get(name)
        if handler is not None:
            handler(payload)

    def _on_worker_job_failed(self, name: str, code: str, message: str) -> None:
        if self._closed:
            return
        _log.warning("gui_job_failed", job=name, code=code, message=message)
        self._busy_jobs.discard(name)
        if name == "open":
            holder = message.split(LOCKED_BY, 1)[1] if LOCKED_BY in message else ""
            self._lock_note = f"lock: busy ({holder})" if code == "camera_busy" else ""
            self._reset_camera_state()
        elif name in ("apply", "set_mode", "preset_apply", "reset"):
            self.camera_settings_panel.show_error(message)
        self._show_warning(f"{name}: {message}")
        self._refresh_ui_state()

    def _on_worker_frame_ready(self, image: QImage, stats: dict[str, Any]) -> None:
        if self._closed:
            return
        overlay = stats.get("clip_overlay")
        view = QRect(
            int(stats.get("roi_x", 0)),
            int(stats.get("roi_y", 0)),
            int(stats.get("width", image.width())),
            int(stats.get("height", image.height())),
        )
        self.live_view.show_frame(image, overlay if isinstance(overlay, QImage) else None, view)
        self.live_view.set_info(
            f"{stats.get('width')}x{stats.get('height')} {stats.get('pixel_format')}  "
            f"frame {stats.get('frame_id')}"
        )
        self.histogram.set_histogram(stats["histogram"], stats["mean"], stats["clipped_pct"])
        fps = float(stats.get("fps", 0.0))
        self.score_panel.set_live_scores(stats["mean"], stats["clipped_pct"], fps)
        self.fps_label.setText(f"{fps:.1f} fps")

    def _on_worker_live_state_changed(self, live: bool) -> None:
        self._live = live
        self.live_action.blockSignals(True)
        self.live_action.setChecked(live)
        self.live_action.blockSignals(False)
        if not live:
            self.fps_label.setText("")
        self._refresh_ui_state()

    def _on_worker_live_error(self, message: str) -> None:
        self._show_warning(f"live view: {message}")

    def _on_live_view_part_area_save_requested(self, x: int, y: int, w: int, h: int) -> None:
        self._submit("part_area_set", x=x, y=y, width=w, height=h)

    def _on_live_view_part_area_clear_requested(self) -> None:
        if self._confirm(
            "Clear part area",
            "Remove this camera's part area? Scores will judge the whole image again.",
        ):
            self._submit("part_area_clear")

    def _on_camera_list_panel_rescan_requested(self) -> None:
        self._submit("discover")

    def _on_camera_list_panel_disconnect_requested(self) -> None:
        if self._camera_id is not None:
            self._submit("close")

    def _on_camera_list_panel_camera_selected(self, serial: str) -> None:
        if serial != self._camera_id:
            self._submit("open", camera_id=serial)

    def _on_camera_settings_panel_patch_requested(self, patch: object) -> None:
        self._submit("apply", patch=patch)

    def _on_camera_settings_panel_mode_requested(self, mode: str) -> None:
        self._submit("set_mode", mode=mode)

    def _on_light_panel_levels_requested(self, levels: dict[str, float]) -> None:
        self._submit("lights_set", levels=levels)

    def _on_run_browser_panel_refresh_requested(self) -> None:
        self._submit("runs", camera_id=self._camera_id)

    def _on_run_browser_panel_run_selected(self, run_id: str) -> None:
        self._submit("run_detail", run_id=run_id)

    def _on_live_action_toggled(self, checked: bool) -> None:
        self._submit("live_start" if checked else "live_stop")

    def _on_capture_action_triggered(self) -> None:
        self._submit("capture")

    def _on_tune_action_triggered(self) -> None:
        self._submit("tune")

    def _on_save_preset_action_triggered(self) -> None:
        choices = list(self._capture_choices)
        selected = self.run_browser_panel.selected_capture_id()
        if selected is not None and selected not in [c[0] for c in choices]:
            choices.insert(0, (selected, selected))
        if not choices:
            self._show_warning(SAVE_NEEDS_CAPTURE)
            return
        dialog = self._ask_preset("save", choices)
        if dialog is not None:
            self._submit(
                "preset_save",
                name=dialog.preset_name(),
                capture_id=dialog.choice_value(),
                overwrite=dialog.overwrite(),
            )

    def _on_apply_preset_action_triggered(self) -> None:
        self._submit("presets_list", camera_id=self._camera_id)

    def _on_reset_action_triggered(self) -> None:
        if self._confirm(
            "Load camera defaults",
            "Load the factory Default settings, then the camera's design mode, and read every "
            "setting back?\n\nThe camera is backed up first. Nothing is written to its flash.",
        ):
            self._submit("reset")

    def _on_lights_off_action_triggered(self) -> None:
        self._submit("lights_off")

    def _on_setup_action_triggered(self) -> None:
        if self._setup_window is not None:
            self._setup_window.raise_()
            self._setup_window.activateWindow()
            return
        window = SetupWindow(self.worker, self._live, parent=self)
        window.finished.connect(self._on_setup_window_finished)
        self._setup_window = window
        window.show()
        self._refresh_ui_state()

    def _on_setup_window_finished(self, _result: int) -> None:
        self._setup_window = None
        self._refresh_ui_state()

    def _on_fingerprint_action_triggered(self) -> None:
        if self._fingerprint_window is not None:
            self._fingerprint_window.raise_()
            self._fingerprint_window.activateWindow()
            return
        window = FingerprintWindow(self.worker, self._live, parent=self)
        window.finished.connect(self._on_fingerprint_window_finished)
        self._fingerprint_window = window
        window.show()
        self._refresh_ui_state()

    def _on_fingerprint_window_finished(self, _result: int) -> None:
        self._fingerprint_window = None
        self._refresh_ui_state()

    def _on_focus_action_triggered(self) -> None:
        if self._focus_window is not None:
            self._focus_window.raise_()
            self._focus_window.activateWindow()
            return
        window = FocusWindow(self.worker, self._live, parent=self)
        window.finished.connect(self._on_focus_window_finished)
        self._focus_window = window
        window.show()
        self._refresh_ui_state()

    def _on_focus_window_finished(self, _result: int) -> None:
        self._focus_window = None
        self._refresh_ui_state()

    # ------------------------------------------------------------ closing
    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt override
        """Stop the workers, close the cameras (releases locks) and turn the lights off."""
        if not self._closed:
            self._closed = True
            self._close_setup_window()
            self._close_fingerprint_window()
            self._close_focus_window()
            self.heartbeat.stop()
            if self.worker_thread.isRunning():
                self.shutdown_requested.emit()  # blocks until the worker has shut down
                self.worker_thread.quit()
                self.worker_thread.wait(THREAD_WAIT_MS)
            _log.info("gui_closed", actor=ACTOR)
        super().closeEvent(event)
