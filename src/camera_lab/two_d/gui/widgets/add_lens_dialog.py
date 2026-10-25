"""The "Add a lens" dialog: the one place a human enters data (Derek 2026-10-25), copied from
the lens barrel — the printed name, the focal length, the mount (picked) and the f-stops
marked on the ring (ticked from the standard list). The fingerprint measures the rest."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGridLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from camera_lab.two_d.catalog.store import MOUNTS, STANDARD_F_STOPS

WINDOW_TITLE = "Add a lens"
HELP_TEXT = (
    "Copy these from the lens barrel. Tick only the f-stops printed on the iris ring "
    "(dots between marks are not stops). The fingerprint measures the rest."
)
NAME_PLACEHOLDER = "as printed, e.g. JAI 6 mm 0628-C3"
STOP_COLUMNS = 6
FOCAL_MIN_MM = 0.5
FOCAL_MAX_MM = 2000.0
MIN_STOPS = 2  # fully open and closed at least


class AddLensDialog(QDialog):
    """Name, focal length, mount and the marked stops; OK only when complete."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._create_widgets()
        self._create_layouts()
        self._connect_signals()
        self._apply_styles()
        self._load_initial_state()

    # --- Widget Construction ---
    def _create_widgets(self) -> None:
        self.help_label = QLabel(HELP_TEXT)
        self.name_edit = QLineEdit()
        self.focal_spin = QDoubleSpinBox()
        self.mount_combo = QComboBox()
        self.stops_widget = QWidget()
        self.stop_boxes: dict[float, QCheckBox] = {
            f: QCheckBox(f"f/{f:g}") for f in STANDARD_F_STOPS
        }
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )

    # --- Layout ---
    def _create_layouts(self) -> None:
        self.main_layout = QVBoxLayout(self)
        self.form_layout = QFormLayout()
        self.stops_layout = QGridLayout(self.stops_widget)
        for i, box in enumerate(self.stop_boxes.values()):
            self.stops_layout.addWidget(box, i // STOP_COLUMNS, i % STOP_COLUMNS)
        self.form_layout.addRow("Name on the barrel", self.name_edit)
        self.form_layout.addRow("Focal length (mm)", self.focal_spin)
        self.form_layout.addRow("Mount", self.mount_combo)
        self.form_layout.addRow("Marked f-stops", self.stops_widget)
        self.main_layout.addWidget(self.help_label)
        self.main_layout.addLayout(self.form_layout)
        self.main_layout.addWidget(self.buttons)

    # --- Signals & Slots ---
    def _connect_signals(self) -> None:
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        self.name_edit.textChanged.connect(self._on_name_edit_text_changed)
        for box in self.stop_boxes.values():
            box.toggled.connect(self._on_stop_box_toggled)

    # --- Styling ---
    def _apply_styles(self) -> None:
        self.setWindowTitle(WINDOW_TITLE)
        self.help_label.setObjectName("secondaryLabel")

    # --- Initial State ---
    def _load_initial_state(self) -> None:
        self.help_label.setWordWrap(True)
        self.name_edit.setPlaceholderText(NAME_PLACEHOLDER)
        self.focal_spin.setRange(FOCAL_MIN_MM, FOCAL_MAX_MM)
        self.focal_spin.setDecimals(1)
        self.focal_spin.setValue(8.0)
        self.mount_combo.addItems(list(MOUNTS))
        self._refresh_ok()

    # --- Public ---
    def values(self) -> tuple[str, float, str, tuple[float, ...]]:
        """(name, focal mm, mount, ticked stops)."""
        stops = tuple(f for f, box in self.stop_boxes.items() if box.isChecked())
        return (
            " ".join(self.name_edit.text().split()),
            float(self.focal_spin.value()),
            self.mount_combo.currentText(),
            stops,
        )

    def _refresh_ok(self) -> None:
        name, _, _, stops = self.values()
        ok = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        ok.setEnabled(bool(name) and len(stops) >= MIN_STOPS)

    # --- Slots ---
    def _on_name_edit_text_changed(self, _text: str) -> None:
        self._refresh_ok()

    def _on_stop_box_toggled(self, _checked: bool) -> None:
        self._refresh_ok()
