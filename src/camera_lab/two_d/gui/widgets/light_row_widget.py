"""One light-channel row: name + slider + percent value."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QSlider, QWidget

from camera_lab.core.lights.base import LightChannel

MARGIN = 6
SPACING = 8
LEVEL_SCALE = 10  # slider steps per percent (0.1 % resolution)
NAME_WIDTH = 130
VALUE_WIDTH = 64


class LightRowWidget(QFrame):
    """Emits ``level_changed(channel_id, percent)`` on user moves only."""

    level_changed = Signal(str, float)

    def __init__(
        self, channel: LightChannel, level_pct: float, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.channel = channel  # data only: the widgets below are built from it
        self._initial_level = level_pct
        self._syncing = False
        self._create_widgets()
        self._create_layouts()
        self._connect_signals()
        self._apply_styles()
        self._load_initial_state()

    # --- Widget Construction ---
    def _create_widgets(self) -> None:
        self.name_label = QLabel(self.channel.name)
        self.level_slider = QSlider(Qt.Orientation.Horizontal)
        self.value_label = QLabel("0.0 %")

    # --- Layout ---
    def _create_layouts(self) -> None:
        self.main_layout = QHBoxLayout(self)
        self.main_layout.setContentsMargins(MARGIN, MARGIN, MARGIN, MARGIN)
        self.main_layout.setSpacing(SPACING)
        self.main_layout.addWidget(self.name_label)
        self.main_layout.addWidget(self.level_slider, 1)
        self.main_layout.addWidget(self.value_label)

    # --- Signals & Slots ---
    def _connect_signals(self) -> None:
        self.level_slider.valueChanged.connect(self._on_level_slider_value_changed)

    # --- Styling ---
    def _apply_styles(self) -> None:
        self.setObjectName("lightRow")
        self.name_label.setMinimumWidth(NAME_WIDTH)
        self.value_label.setMinimumWidth(VALUE_WIDTH)
        self.value_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

    # --- Initial State ---
    def _load_initial_state(self) -> None:
        self.level_slider.setRange(0, round(self.channel.max_pct * LEVEL_SCALE))
        self.name_label.setToolTip(f"{self.channel.colour} (max {self.channel.max_pct:g} %)")
        self.set_level(self._initial_level)

    # --- Public API ---
    @property
    def channel_id(self) -> str:
        """The channel id."""
        return self.channel.id

    def is_editing(self) -> bool:
        """True while the user drags the slider."""
        return self.level_slider.isSliderDown()

    def level(self) -> float:
        """The level shown, in percent."""
        return self.level_slider.value() / LEVEL_SCALE

    def set_level(self, percent: float) -> None:
        """Show ``percent`` without emitting."""
        self._syncing = True
        self.level_slider.setValue(round(percent * LEVEL_SCALE))
        self.value_label.setText(f"{percent:.1f} %")
        self._syncing = False

    # --- Slots ---
    def _on_level_slider_value_changed(self, position: int) -> None:
        percent = position / LEVEL_SCALE
        self.value_label.setText(f"{percent:.1f} %")
        if not self._syncing:
            self.level_changed.emit(self.channel.id, percent)
