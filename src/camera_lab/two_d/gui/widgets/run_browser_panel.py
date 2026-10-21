"""Runs tab: run rows, the captures of the selected run, and a 2-capture compare."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (
    QAbstractButton,
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from camera_lab.core.paths import safe_join, two_d_runs_root
from camera_lab.core.records import CaptureSidecar, RunSummary
from camera_lab.two_d.gui.widgets.layout_utils import remove_all_widgets

MARGIN = 8
SPACING = 6
ROW_MARGIN = 4
RUN_THUMB_SIZE = 56
CAPTURE_THUMB_SIZE = 72
COMPARE_SIZE = 220
COMPARE_MIN_SIZE = 110
MAX_COMPARED = 2
RUNS_HEIGHT = 170
CAPTURES_HEIGHT = 130
RUN_ID_PROPERTY = "run_id"
CAPTURE_ID_PROPERTY = "capture_id"
NO_PREVIEW_TEXT = "no preview"
COMPARE_HINT = "Select two captures to compare them side by side"


def preview_path(capture: CaptureSidecar, runs_root: Path | None = None) -> Path | None:
    """The preview JPEG of a capture, if the file exists."""
    if capture.preview is None:
        return None
    run_id = capture.capture_id.split("/", 1)[0]
    path = safe_join(runs_root or two_d_runs_root(), run_id, *capture.preview.split("/"))
    return path if path.is_file() else None


def best_preview_path(summary: RunSummary, runs_root: Path | None = None) -> Path | None:
    """The preview JPEG of a run's best capture, if any."""
    if summary.best_capture_id is None:
        return None
    run_id, name = summary.best_capture_id.split("/", 1)
    path = safe_join(runs_root or two_d_runs_root(), run_id, "previews", f"{name}.jpg")
    return path if path.is_file() else None


def capture_caption(capture: CaptureSidecar) -> str:
    """One-line description: number, composite score, exposure and gain."""
    comp = capture.scores.get("composite")
    exp = capture.settings.get("exposure_us")
    gain = capture.settings.get("gain_db")
    parts = [capture.capture_id.rsplit("/", 1)[1]]
    if isinstance(comp, int | float):
        parts.append(f"score {comp:.1f}")
    if isinstance(exp, int | float):
        parts.append(f"exp {exp:g} us")
    if isinstance(gain, int | float):
        parts.append(f"gain {gain:g} dB")
    return "  ".join(parts)


class RunBrowserPanel(QWidget):
    """Run browser. Emits ``run_selected`` and ``capture_selected``; never reads the service."""

    refresh_requested = Signal()
    run_selected = Signal(str)
    capture_selected = Signal(str)
    compare_changed = Signal(list)

    def __init__(self, runs_root: Path | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._runs_root = runs_root  # data only; None = the default runs folder
        self._create_widgets()
        self._create_layouts()
        self._connect_signals()
        self._apply_styles()
        self._load_initial_state()

    # --- Widget Construction ---
    def _create_widgets(self) -> None:
        self.refresh_button = QPushButton("Refresh runs")
        self.runs_container = QWidget()
        self.runs_scroll = QScrollArea()
        self.run_button_group = QButtonGroup(self)
        self.captures_title_label = QLabel("Captures")
        self.captures_container = QWidget()
        self.captures_scroll = QScrollArea()
        self.compare_title_label = QLabel(COMPARE_HINT)
        self.compare_image_a = QLabel("A")
        self.compare_image_b = QLabel("B")
        self.compare_caption_a = QLabel("")
        self.compare_caption_b = QLabel("")
        self._run_buttons: dict[str, QPushButton] = {}
        self._capture_buttons: dict[str, QPushButton] = {}
        self._captures: dict[str, CaptureSidecar] = {}
        self._compared: list[str] = []

    # --- Layout ---
    def _create_layouts(self) -> None:
        self.main_layout = QVBoxLayout(self)
        self.runs_layout = QVBoxLayout(self.runs_container)
        self.captures_layout = QHBoxLayout(self.captures_container)
        self.compare_layout = QHBoxLayout()
        self.compare_a_layout = QVBoxLayout()
        self.compare_b_layout = QVBoxLayout()

        self.main_layout.setContentsMargins(MARGIN, MARGIN, MARGIN, MARGIN)
        self.main_layout.setSpacing(SPACING)
        for layout in (self.runs_layout, self.captures_layout):
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(SPACING)
        self.runs_scroll.setWidget(self.runs_container)
        self.runs_scroll.setWidgetResizable(True)
        self.runs_scroll.setMinimumHeight(RUNS_HEIGHT)
        self.captures_scroll.setWidget(self.captures_container)
        self.captures_scroll.setWidgetResizable(True)
        self.captures_scroll.setFixedHeight(CAPTURES_HEIGHT)
        self.compare_a_layout.addWidget(self.compare_image_a, 1)
        self.compare_a_layout.addWidget(self.compare_caption_a)
        self.compare_b_layout.addWidget(self.compare_image_b, 1)
        self.compare_b_layout.addWidget(self.compare_caption_b)
        self.compare_layout.addLayout(self.compare_a_layout, 1)
        self.compare_layout.addLayout(self.compare_b_layout, 1)
        self.main_layout.addWidget(self.refresh_button)
        self.main_layout.addWidget(self.runs_scroll, 1)
        self.main_layout.addWidget(self.captures_title_label)
        self.main_layout.addWidget(self.captures_scroll)
        self.main_layout.addWidget(self.compare_title_label)
        self.main_layout.addLayout(self.compare_layout, 1)

    # --- Signals & Slots ---
    def _connect_signals(self) -> None:
        self.refresh_button.clicked.connect(self._on_refresh_button_clicked)
        self.run_button_group.buttonClicked.connect(self._on_run_button_group_clicked)

    # --- Styling ---
    def _apply_styles(self) -> None:
        self.setObjectName("runBrowserPanel")
        self.captures_title_label.setObjectName("secondaryLabel")
        self.compare_title_label.setObjectName("secondaryLabel")
        for label in (self.compare_image_a, self.compare_image_b):
            label.setObjectName("comparePreview")
        for label in (self.compare_caption_a, self.compare_caption_b):
            label.setObjectName("secondaryLabel")

    # --- Initial State ---
    def _load_initial_state(self) -> None:
        self.run_button_group.setExclusive(True)
        for label in (self.compare_image_a, self.compare_image_b):
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label.setMinimumSize(COMPARE_MIN_SIZE, COMPARE_MIN_SIZE)

    # --- Dynamic rows: factory + populate + clear ---
    def _create_run_row_widget(self, summary: RunSummary) -> QFrame:
        """Factory: ONE run row (thumbnail + run id + method + best score)."""
        row = QFrame()
        row.setObjectName("runRow")
        thumb_label = QLabel(NO_PREVIEW_TEXT)
        thumb_label.setObjectName("runThumb")
        thumb_label.setFixedSize(RUN_THUMB_SIZE, RUN_THUMB_SIZE)
        thumb_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        path = best_preview_path(summary, self._runs_root)
        if path is not None:
            pixmap = QPixmap(str(path))
            if not pixmap.isNull():
                thumb_label.setPixmap(
                    pixmap.scaled(
                        RUN_THUMB_SIZE,
                        RUN_THUMB_SIZE,
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                )
        best = "-" if summary.best_score is None else f"{summary.best_score:.1f}"
        button = QPushButton(
            f"{summary.run_id}\n{summary.method} | {summary.capture_count} captures | best {best}"
        )
        button.setObjectName("cameraButton")
        button.setCheckable(True)
        button.setProperty(RUN_ID_PROPERTY, summary.run_id)
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(ROW_MARGIN, ROW_MARGIN, ROW_MARGIN, ROW_MARGIN)
        row_layout.addWidget(thumb_label)
        row_layout.addWidget(button, 1)
        return row

    def populate_runs(self, runs: list[RunSummary]) -> None:
        """Clear and rebuild the run list (newest first, as given)."""
        self._clear_run_rows()
        for summary in runs:
            row = self._create_run_row_widget(summary)
            button = row.findChild(QPushButton, "cameraButton")
            if button is not None:
                self.run_button_group.addButton(button)
                self._run_buttons[summary.run_id] = button
            self.runs_layout.addWidget(row)
        self.runs_layout.addStretch(1)

    def _clear_run_rows(self) -> None:
        """Remove all run rows."""
        for button in list(self._run_buttons.values()):
            self.run_button_group.removeButton(button)
        self._run_buttons.clear()
        remove_all_widgets(self.runs_layout)

    def clear_runs(self) -> None:
        """Public clear (also clears captures and the compare view)."""
        self._clear_run_rows()
        self.clear_captures()

    def _create_capture_thumb_widget(self, capture: CaptureSidecar) -> QFrame:
        """Factory: ONE capture thumbnail (checkable, for the compare view)."""
        frame = QFrame()
        frame.setObjectName("captureThumb")
        button = QPushButton(capture.capture_id.rsplit("/", 1)[1])
        button.setObjectName("captureThumbButton")
        button.setCheckable(True)
        button.setProperty(CAPTURE_ID_PROPERTY, capture.capture_id)
        button.setToolTip(capture_caption(capture))
        button.setIconSize(QSize(CAPTURE_THUMB_SIZE, CAPTURE_THUMB_SIZE))
        path = preview_path(capture, self._runs_root)
        if path is not None:
            pixmap = QPixmap(str(path))
            if not pixmap.isNull():
                button.setIcon(QIcon(pixmap))
        frame_layout = QVBoxLayout(frame)
        frame_layout.setContentsMargins(ROW_MARGIN, ROW_MARGIN, ROW_MARGIN, ROW_MARGIN)
        frame_layout.addWidget(button)
        return frame

    def populate_captures(self, captures: list[CaptureSidecar]) -> None:
        """Clear and rebuild the capture thumbnails of the selected run."""
        self.clear_captures()
        for capture in captures:
            frame = self._create_capture_thumb_widget(capture)
            button = frame.findChild(QPushButton, "captureThumbButton")
            if button is not None:
                self._connect_capture_thumb_signals(button)
                self._capture_buttons[capture.capture_id] = button
            self._captures[capture.capture_id] = capture
            self.captures_layout.addWidget(frame)
        self.captures_layout.addStretch(1)

    def _connect_capture_thumb_signals(self, button: QPushButton) -> None:
        """Wire one generated thumbnail (they do not exist yet in ``_connect_signals``)."""
        button.toggled.connect(self._on_capture_thumb_button_toggled)

    def _clear_capture_thumbs(self) -> None:
        """Remove all capture thumbnails."""
        self._capture_buttons.clear()
        self._captures.clear()
        remove_all_widgets(self.captures_layout)

    def clear_captures(self) -> None:
        """Public clear (thumbnails + compare view)."""
        self._clear_capture_thumbs()
        self._compared.clear()
        self._refresh_compare()

    # --- Public API ---
    def run_ids(self) -> list[str]:
        """Run ids in display order."""
        return list(self._run_buttons)

    def capture_ids(self) -> list[str]:
        """Capture ids of the selected run."""
        return list(self._capture_buttons)

    def compared_ids(self) -> list[str]:
        """The captures currently compared (0-2)."""
        return list(self._compared)

    def selected_capture_id(self) -> str | None:
        """The most recently selected capture (for 'Save preset...')."""
        return self._compared[-1] if self._compared else None

    def select_capture(self, capture_id: str, selected: bool = True) -> None:
        """Select/deselect a capture thumbnail (same as clicking it)."""
        button = self._capture_buttons.get(capture_id)
        if button is not None:
            button.setChecked(selected)

    # --- Helpers ---
    def _refresh_compare(self) -> None:
        slots = (
            (self.compare_image_a, self.compare_caption_a, "A"),
            (self.compare_image_b, self.compare_caption_b, "B"),
        )
        for i, (image_label, caption_label, tag) in enumerate(slots):
            capture = self._captures.get(self._compared[i]) if i < len(self._compared) else None
            if capture is None:
                image_label.setPixmap(QPixmap())
                image_label.setText(tag)
                caption_label.setText("")
                continue
            path = preview_path(capture, self._runs_root)
            pixmap = QPixmap(str(path)) if path is not None else QPixmap()
            if pixmap.isNull():
                image_label.setPixmap(QPixmap())
                image_label.setText(NO_PREVIEW_TEXT)
            else:
                image_label.setPixmap(
                    pixmap.scaled(
                        COMPARE_SIZE,
                        COMPARE_SIZE,
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                )
            caption_label.setText(f"{tag}: {capture_caption(capture)}")
        self.compare_title_label.setText(
            "Comparing A and B" if len(self._compared) == MAX_COMPARED else COMPARE_HINT
        )

    # --- Slots ---
    def _on_refresh_button_clicked(self) -> None:
        self.refresh_requested.emit()

    def _on_run_button_group_clicked(self, button: QAbstractButton) -> None:
        run_id = button.property(RUN_ID_PROPERTY)
        if isinstance(run_id, str):
            self.run_selected.emit(run_id)

    def _on_capture_thumb_button_toggled(self, checked: bool) -> None:
        sender = self.sender()
        capture_id = sender.property(CAPTURE_ID_PROPERTY) if sender is not None else None
        if not isinstance(capture_id, str):
            return
        if checked:
            self._compared.append(capture_id)
            while len(self._compared) > MAX_COMPARED:
                oldest = self._compared.pop(0)
                button = self._capture_buttons.get(oldest)
                if button is not None:
                    button.blockSignals(True)
                    button.setChecked(False)
                    button.blockSignals(False)
            self.capture_selected.emit(capture_id)
        elif capture_id in self._compared:
            self._compared.remove(capture_id)
        self._refresh_compare()
        self.compare_changed.emit(list(self._compared))
