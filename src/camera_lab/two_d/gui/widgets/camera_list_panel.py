"""Left panel: the discovered cameras, a Rescan and a Disconnect button."""

from __future__ import annotations

from PySide6.QtCore import Signal
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

from camera_lab.two_d.gui.widgets.layout_utils import remove_all_widgets
from camera_lab.two_d.models import CameraInfo

MARGIN = 8
SPACING = 6
ROW_MARGIN = 6
ROW_SPACING = 2
PANEL_MIN_WIDTH = 220
CAMERA_ID_PROPERTY = "camera_id"
STATE_OPEN = "open"
STATE_IDLE = "idle"


class CameraListPanel(QWidget):
    """Camera rows built from ``discover()`` results, plus Rescan and Disconnect buttons."""

    camera_selected = Signal(str)
    rescan_requested = Signal()
    disconnect_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._create_widgets()
        self._create_layouts()
        self._connect_signals()
        self._apply_styles()
        self._load_initial_state()

    # --- Widget Construction ---
    def _create_widgets(self) -> None:
        self.title_label = QLabel("Cameras")
        self.rescan_button = QPushButton("Rescan")
        self.disconnect_button = QPushButton("Disconnect")
        self.camera_container = QWidget()
        self.camera_scroll = QScrollArea()
        self.empty_label = QLabel("No cameras found")
        self.camera_button_group = QButtonGroup(self)
        self._buttons: dict[str, QPushButton] = {}
        self._state_labels: dict[str, QLabel] = {}

    # --- Layout ---
    def _create_layouts(self) -> None:
        self.main_layout = QVBoxLayout(self)
        self.header_layout = QHBoxLayout()
        self.camera_layout = QVBoxLayout(self.camera_container)

        self.main_layout.setContentsMargins(MARGIN, MARGIN, MARGIN, MARGIN)
        self.main_layout.setSpacing(SPACING)
        self.header_layout.addWidget(self.title_label)
        self.header_layout.addStretch(1)
        self.header_layout.addWidget(self.disconnect_button)
        self.header_layout.addWidget(self.rescan_button)
        self.camera_layout.setContentsMargins(0, 0, 0, 0)
        self.camera_layout.setSpacing(SPACING)
        self.camera_scroll.setWidget(self.camera_container)
        self.camera_scroll.setWidgetResizable(True)
        self.main_layout.addLayout(self.header_layout)
        self.main_layout.addWidget(self.empty_label)
        self.main_layout.addWidget(self.camera_scroll, 1)

    # --- Signals & Slots ---
    def _connect_signals(self) -> None:
        self.rescan_button.clicked.connect(self._on_rescan_button_clicked)
        self.disconnect_button.clicked.connect(self._on_disconnect_button_clicked)
        self.camera_button_group.buttonClicked.connect(self._on_camera_button_group_clicked)

    # --- Styling ---
    def _apply_styles(self) -> None:
        self.setObjectName("cameraListPanel")
        self.title_label.setObjectName("panelTitle")
        self.empty_label.setObjectName("secondaryLabel")
        self.setMinimumWidth(PANEL_MIN_WIDTH)

    # --- Initial State ---
    def _load_initial_state(self) -> None:
        self.camera_button_group.setExclusive(True)
        self.empty_label.setVisible(True)
        self.disconnect_button.setEnabled(False)
        self.disconnect_button.setToolTip(
            "Release the open camera (for Arena / eBUS or another tool); not while a job, "
            "setup or fingerprint is running"
        )

    # --- Dynamic rows: factory + populate + clear ---
    def _create_camera_row_widget(self, info: CameraInfo) -> QFrame:
        """Factory: ONE camera row (button with model/serial/vendor + a state label)."""
        row = QFrame()
        row.setObjectName("cameraRow")
        button = QPushButton(f"{info.model}\n{info.serial}  |  {info.vendor}")
        button.setObjectName("cameraButton")
        button.setCheckable(True)
        button.setProperty(CAMERA_ID_PROPERTY, info.serial)
        state_label = QLabel(STATE_IDLE)
        state_label.setObjectName("cameraStateLabel")
        row_layout = QVBoxLayout(row)
        row_layout.setContentsMargins(ROW_MARGIN, ROW_MARGIN, ROW_MARGIN, ROW_MARGIN)
        row_layout.setSpacing(ROW_SPACING)
        row_layout.addWidget(button)
        row_layout.addWidget(state_label)
        return row

    def populate_cameras(self, cameras: list[CameraInfo]) -> None:
        """Clear the list and build one row per camera."""
        self._clear_camera_list()
        for info in cameras:
            row = self._create_camera_row_widget(info)
            button = row.findChild(QPushButton, "cameraButton")
            state_label = row.findChild(QLabel, "cameraStateLabel")
            if button is not None and state_label is not None:
                self.camera_button_group.addButton(button)
                self._buttons[info.serial] = button
                self._state_labels[info.serial] = state_label
            self.camera_layout.addWidget(row)
        self.camera_layout.addStretch(1)
        self.empty_label.setVisible(not cameras)

    def _clear_camera_list(self) -> None:
        """Remove all camera rows."""
        for button in list(self._buttons.values()):
            self.camera_button_group.removeButton(button)
        self._buttons.clear()
        self._state_labels.clear()
        remove_all_widgets(self.camera_layout)

    def clear_cameras(self) -> None:
        """Public clear (used by tests and rescan)."""
        self._clear_camera_list()
        self.empty_label.setVisible(True)

    # --- Public state ---
    def camera_ids(self) -> list[str]:
        """Serials in the list, in display order."""
        return list(self._buttons)

    def set_open_camera(self, serial: str | None) -> None:
        """Mark ``serial`` as the open camera (``None`` = none open)."""
        for cid, label in self._state_labels.items():
            label.setText(STATE_OPEN if cid == serial else STATE_IDLE)
        button = self._buttons.get(serial) if serial is not None else None
        if button is not None:
            button.setChecked(True)
        else:
            self.camera_button_group.setExclusive(False)
            for b in self._buttons.values():
                b.setChecked(False)
            self.camera_button_group.setExclusive(True)

    def set_disconnect_enabled(self, enabled: bool) -> None:
        """Enable Disconnect (only with a camera open and nothing running on it)."""
        self.disconnect_button.setEnabled(enabled)

    # --- Slots ---
    def _on_rescan_button_clicked(self) -> None:
        self.rescan_requested.emit()

    def _on_disconnect_button_clicked(self) -> None:
        self.disconnect_requested.emit()

    def _on_camera_button_group_clicked(self, button: QAbstractButton) -> None:
        serial = button.property(CAMERA_ID_PROPERTY)
        if isinstance(serial, str):
            self.camera_selected.emit(serial)
