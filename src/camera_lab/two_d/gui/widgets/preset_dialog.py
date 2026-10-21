"""Save / Apply preset dialog."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

from PySide6.QtCore import QRegularExpression
from PySide6.QtGui import QRegularExpressionValidator
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from camera_lab.core.paths import PRESET_NAME_RE

MARGIN = 12
SPACING = 8
MIN_WIDTH = 380
Mode = Literal["save", "apply"]


class PresetDialog(QDialog):
    """``save``: name + source capture (+ overwrite). ``apply``: pick a preset.

    ``choices`` are ``(value, label)`` pairs: capture ids for save, preset names for apply.
    """

    def __init__(
        self,
        mode: Mode,
        choices: Sequence[tuple[str, str]],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.mode: Mode = mode  # data only: the widgets below are built from it
        self._choices = list(choices)
        self._create_widgets()
        self._create_layouts()
        self._connect_signals()
        self._apply_styles()
        self._load_initial_state()

    # --- Widget Construction ---
    def _create_widgets(self) -> None:
        self.hint_label = QLabel("")
        self.name_label = QLabel("Preset name")
        self.name_input = QLineEdit()
        self.choice_label = QLabel("")
        self.choice_combo = QComboBox()
        self.overwrite_checkbox = QCheckBox("Overwrite if it exists")
        self.button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )

    # --- Layout ---
    def _create_layouts(self) -> None:
        self.main_layout = QVBoxLayout(self)
        self.form_layout = QFormLayout()
        self.main_layout.setContentsMargins(MARGIN, MARGIN, MARGIN, MARGIN)
        self.main_layout.setSpacing(SPACING)
        self.form_layout.addRow(self.name_label, self.name_input)
        self.form_layout.addRow(self.choice_label, self.choice_combo)
        self.form_layout.addRow(self.overwrite_checkbox)
        self.main_layout.addWidget(self.hint_label)
        self.main_layout.addLayout(self.form_layout)
        self.main_layout.addWidget(self.button_box)

    # --- Signals & Slots ---
    def _connect_signals(self) -> None:
        self.button_box.accepted.connect(self.accept)
        self.button_box.rejected.connect(self.reject)
        self.name_input.textChanged.connect(self._on_name_input_text_changed)
        self.choice_combo.currentIndexChanged.connect(self._on_choice_combo_current_index_changed)

    # --- Styling ---
    def _apply_styles(self) -> None:
        self.setObjectName("presetDialog")
        self.hint_label.setObjectName("secondaryLabel")
        self.hint_label.setWordWrap(True)
        self.setMinimumWidth(MIN_WIDTH)

    # --- Initial State ---
    def _load_initial_state(self) -> None:
        saving = self.mode == "save"
        self.setWindowTitle("Save preset" if saving else "Apply preset")
        self.hint_label.setText(
            "Save the settings and light levels of a capture as a named preset."
            if saving
            else "Apply a preset's settings and light levels (through the safety limits)."
        )
        self.choice_label.setText("From capture" if saving else "Preset")
        for value, label in self._choices:
            self.choice_combo.addItem(label, value)
        self.name_label.setVisible(saving)
        self.name_input.setVisible(saving)
        self.overwrite_checkbox.setVisible(saving)
        self.name_input.setValidator(
            QRegularExpressionValidator(QRegularExpression(PRESET_NAME_RE.pattern))
        )
        self.name_input.setPlaceholderText("a-z 0-9 _ - (max 40)")
        self._update_ok_enabled()

    # --- Public API ---
    def preset_name(self) -> str:
        """The name to save as (save mode) or the chosen preset (apply mode)."""
        return self.name_input.text() if self.mode == "save" else self.choice_value()

    def choice_value(self) -> str:
        """The value of the chosen entry (capture id or preset name)."""
        data = self.choice_combo.currentData()
        return data if isinstance(data, str) else ""

    def overwrite(self) -> bool:
        """True if overwriting an existing preset was allowed."""
        return self.overwrite_checkbox.isChecked()

    # --- Helpers ---
    def _update_ok_enabled(self) -> None:
        ok = self.button_box.button(QDialogButtonBox.StandardButton.Ok)
        valid = bool(self.choice_value()) and (
            self.mode == "apply" or PRESET_NAME_RE.fullmatch(self.name_input.text()) is not None
        )
        ok.setEnabled(valid)

    # --- Slots ---
    def _on_name_input_text_changed(self, text: str) -> None:
        self._update_ok_enabled()

    def _on_choice_combo_current_index_changed(self, index: int) -> None:
        self._update_ok_enabled()
