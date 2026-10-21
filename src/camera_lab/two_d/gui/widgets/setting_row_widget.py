"""One camera-setting row: label + slider + spinbox + unit (or a combo for enums)."""

from __future__ import annotations

import math

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFrame,
    QGridLayout,
    QLabel,
    QSlider,
    QWidget,
)

from camera_lab.two_d.models import SettingSpec

MARGIN = 6
SPACING = 6
SLIDER_STEPS = 1000
LOG_SCALE_MIN_RATIO = 100.0  # ranges spanning more than this use a log slider
SPINBOX_HARD_LIMIT = 1.0e9  # typed values outside the camera range must reach the service
MAX_DECIMALS = 4
FLOAT_EPS = 1e-9
COL_NAME, COL_CONTROL, COL_SPINBOX, COL_UNIT = 0, 1, 2, 3
ROW_CONTROLS, ROW_REASON = 0, 1
COLUMN_COUNT = 4
SPINBOX_WIDTH = 100
UNIT_WIDTH = 32
NAME_WIDTH = 120
LEVEL_ERROR = "error"
LEVEL_WARNING = "warning"
LABELS: dict[str, str] = {
    "exposure_us": "Exposure",
    "gain_db": "Gain",
    "gamma": "Gamma",
    "wb_red": "WB red",
    "wb_green": "WB green",
    "wb_blue": "WB blue",
    "frame_rate_hz": "Frame rate",
    "pixel_format": "Pixel format",
    "trigger_mode": "Trigger",
    "roi_x": "ROI x",
    "roi_y": "ROI y",
    "roi_width": "ROI width",
    "roi_height": "ROI height",
}


def label_for(name: str) -> str:
    """Human label for a setting name (advanced names are shown as they are)."""
    return LABELS.get(name, name)


def decimals_for(spec: SettingSpec) -> int:
    """Spinbox decimals from the increment (0 for integer settings)."""
    if spec.kind == "int":
        return 0
    inc = spec.increment
    if inc is None or inc <= 0:
        return 2
    return min(MAX_DECIMALS, max(0, math.ceil(-math.log10(inc) - FLOAT_EPS)))


class SettingRowWidget(QFrame):
    """A row for one setting. Emits ``value_changed(name, value)`` on user edits only."""

    value_changed = Signal(str, object)

    def __init__(
        self,
        spec: SettingSpec,
        value: float | str | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.spec = spec  # data only: the widgets below are built from it
        self._initial_value = value
        self._syncing = False
        self._create_widgets()
        self._create_layouts()
        self._connect_signals()
        self._apply_styles()
        self._load_initial_state()

    # --- Widget Construction ---
    def _create_widgets(self) -> None:
        self.name_label = QLabel(label_for(self.spec.name))
        self.value_slider = QSlider(Qt.Orientation.Horizontal)
        self.value_spinbox = QDoubleSpinBox()
        self.unit_label = QLabel(self.spec.unit)
        self.option_combo = QComboBox()
        self.reason_label = QLabel("")

    # --- Layout ---
    def _create_layouts(self) -> None:
        self.main_layout = QGridLayout(self)
        self.main_layout.setContentsMargins(MARGIN, MARGIN, MARGIN, MARGIN)
        self.main_layout.setHorizontalSpacing(SPACING)
        self.main_layout.setVerticalSpacing(0)
        self.main_layout.addWidget(self.name_label, ROW_CONTROLS, COL_NAME)
        self.main_layout.addWidget(self.value_slider, ROW_CONTROLS, COL_CONTROL)
        self.main_layout.addWidget(self.option_combo, ROW_CONTROLS, COL_CONTROL)
        self.main_layout.addWidget(self.value_spinbox, ROW_CONTROLS, COL_SPINBOX)
        self.main_layout.addWidget(self.unit_label, ROW_CONTROLS, COL_UNIT)
        self.main_layout.addWidget(self.reason_label, ROW_REASON, COL_NAME, 1, COLUMN_COUNT)
        self.main_layout.setColumnStretch(COL_CONTROL, 1)

    # --- Signals & Slots ---
    def _connect_signals(self) -> None:
        self.value_slider.valueChanged.connect(self._on_value_slider_value_changed)
        self.value_spinbox.valueChanged.connect(self._on_value_spinbox_value_changed)
        self.option_combo.currentTextChanged.connect(self._on_option_combo_current_text_changed)

    # --- Styling ---
    def _apply_styles(self) -> None:
        self.setObjectName("settingRow")
        self.name_label.setObjectName("settingName")
        self.unit_label.setObjectName("unitLabel")
        self.reason_label.setObjectName("reasonLabel")
        self.name_label.setMinimumWidth(NAME_WIDTH)
        self.unit_label.setMinimumWidth(UNIT_WIDTH)
        self.value_spinbox.setFixedWidth(SPINBOX_WIDTH)

    # --- Initial State ---
    def _load_initial_state(self) -> None:
        is_enum = self.spec.kind == "enum"
        self.option_combo.setVisible(is_enum)
        self.value_slider.setVisible(not is_enum)
        self.value_spinbox.setVisible(not is_enum)
        self.unit_label.setVisible(not is_enum)
        self.reason_label.setVisible(False)
        tips = []
        if self.spec.affects_calibration:
            tips.append("changing this can invalidate the calibration")
        if self.spec.requires_stream_stop:
            tips.append("the stream is stopped while this changes")
        self.name_label.setToolTip("; ".join(tips))
        if is_enum:
            self.option_combo.addItems(list(self.spec.options))
        else:
            self.value_slider.setRange(0, SLIDER_STEPS)
            self.value_spinbox.setKeyboardTracking(False)
            self.value_spinbox.setRange(-SPINBOX_HARD_LIMIT, SPINBOX_HARD_LIMIT)
            self.value_spinbox.setDecimals(decimals_for(self.spec))
            self.value_spinbox.setSingleStep(self.spec.increment or 1.0)
        if self._initial_value is not None:
            self.set_value(self._initial_value)

    # --- Public API ---
    @property
    def name(self) -> str:
        """The setting name."""
        return self.spec.name

    def is_editing(self) -> bool:
        """True while the user drags the slider (read-backs must not move it)."""
        return self.value_slider.isSliderDown()

    def current_value(self) -> float | str:
        """The value currently shown."""
        if self.spec.kind == "enum":
            return self.option_combo.currentText()
        return self.value_spinbox.value()

    def set_value(self, value: float | str) -> None:
        """Show ``value`` without emitting ``value_changed``."""
        self._syncing = True
        try:
            if self.spec.kind == "enum":
                if isinstance(value, str):
                    self.option_combo.setCurrentText(value)
            elif not isinstance(value, str):
                self.value_spinbox.setValue(float(value))
                self.value_slider.setValue(self._slider_from_value(float(value)))
        finally:
            self._syncing = False

    def update_spec(self, spec: SettingSpec) -> None:
        """Adopt a new range/options (the camera moved its limits) and keep the value."""
        keep = self.current_value()
        self.spec = spec
        self.unit_label.setText(spec.unit)
        if spec.kind == "enum":
            self._syncing = True
            self.option_combo.clear()
            self.option_combo.addItems(list(spec.options))
            self._syncing = False
        else:
            self.value_spinbox.setSingleStep(spec.increment or 1.0)
            self.value_spinbox.setDecimals(decimals_for(spec))
        self.set_value(keep)

    def show_result(self, status: str, reason: str | None) -> None:
        """Show the outcome next to the row: rejected/clamped reasons, or clear it."""
        if status == "rejected":
            self._show_reason(f"rejected: {reason or 'not allowed'}", LEVEL_ERROR)
        elif status == "clamped":
            self._show_reason(f"adjusted: {reason or 'rounded to a valid value'}", LEVEL_WARNING)
        else:
            self.clear_reason()

    def clear_reason(self) -> None:
        """Hide the reason text."""
        self.reason_label.setText("")
        self.reason_label.setVisible(False)

    def reason_text(self) -> str:
        """The reason currently shown ('' if none)."""
        return self.reason_label.text()

    # --- Helpers ---
    def _show_reason(self, text: str, level: str) -> None:
        self.reason_label.setProperty("level", level)
        self.reason_label.style().unpolish(self.reason_label)
        self.reason_label.style().polish(self.reason_label)
        self.reason_label.setText(text)
        self.reason_label.setVisible(True)

    def _uses_log_scale(self) -> bool:
        lo, hi = self.spec.min, self.spec.max
        return lo is not None and hi is not None and lo > 0 and hi / lo >= LOG_SCALE_MIN_RATIO

    def _slider_from_value(self, value: float) -> int:
        lo, hi = self.spec.min, self.spec.max
        if lo is None or hi is None or hi <= lo:
            return 0
        v = min(hi, max(lo, value))
        frac = (
            math.log(v / lo) / math.log(hi / lo) if self._uses_log_scale() else (v - lo) / (hi - lo)
        )
        return round(frac * SLIDER_STEPS)

    def value_from_slider(self, position: int) -> float:
        """The setting value for a slider position (snapped to the increment)."""
        lo, hi = self.spec.min, self.spec.max
        if lo is None or hi is None:
            return float(position)
        frac = position / SLIDER_STEPS
        value = lo * (hi / lo) ** frac if self._uses_log_scale() else lo + frac * (hi - lo)
        inc = self.spec.increment
        if inc:
            value = lo + round((value - lo) / inc) * inc
        value = min(hi, max(lo, value))
        return round(value, decimals_for(self.spec))

    # --- Slots ---
    def _on_value_slider_value_changed(self, position: int) -> None:
        if self._syncing:
            return
        value = self.value_from_slider(position)
        self._syncing = True
        self.value_spinbox.setValue(value)
        self._syncing = False
        self.value_changed.emit(self.spec.name, value)

    def _on_value_spinbox_value_changed(self, value: float) -> None:
        if self._syncing:
            return
        self._syncing = True
        self.value_slider.setValue(self._slider_from_value(value))
        self._syncing = False
        self.value_changed.emit(self.spec.name, value)

    def _on_option_combo_current_text_changed(self, text: str) -> None:
        if self._syncing or not text:
            return
        self.value_changed.emit(self.spec.name, text)
