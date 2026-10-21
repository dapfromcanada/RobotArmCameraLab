"""Light tab: one slider row per light channel."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import QLabel, QScrollArea, QVBoxLayout, QWidget

from camera_lab.core.lights.base import LightChannel
from camera_lab.two_d.gui.constants import DEBOUNCE_MS
from camera_lab.two_d.gui.widgets.layout_utils import remove_all_widgets
from camera_lab.two_d.gui.widgets.light_row_widget import LightRowWidget

MARGIN = 8
SPACING = 6
MIN_CHANNEL_MAX_PCT = 0.1  # LightChannel needs max_pct > 0; a 0 % safety limit shows a dead slider
NO_LIGHTS_TEXT = "No light controller"


class LightPanel(QWidget):
    """Channel rows from ``list_lights()``; emits coalesced ``levels_requested``."""

    levels_requested = Signal(dict)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._create_widgets()
        self._create_layouts()
        self._connect_signals()
        self._apply_styles()
        self._load_initial_state()

    # --- Widget Construction ---
    def _create_widgets(self) -> None:
        self.status_label = QLabel(NO_LIGHTS_TEXT)
        self.rows_container = QWidget()
        self.rows_scroll = QScrollArea()
        self.reason_label = QLabel("")
        self.debounce_timer = QTimer(self)
        self._rows: dict[str, LightRowWidget] = {}
        self._pending: dict[str, float] = {}

    # --- Layout ---
    def _create_layouts(self) -> None:
        self.main_layout = QVBoxLayout(self)
        self.rows_layout = QVBoxLayout(self.rows_container)
        self.main_layout.setContentsMargins(MARGIN, MARGIN, MARGIN, MARGIN)
        self.main_layout.setSpacing(SPACING)
        self.rows_layout.setContentsMargins(0, 0, 0, 0)
        self.rows_layout.setSpacing(SPACING)
        self.rows_scroll.setWidget(self.rows_container)
        self.rows_scroll.setWidgetResizable(True)
        self.main_layout.addWidget(self.status_label)
        self.main_layout.addWidget(self.rows_scroll, 1)
        self.main_layout.addWidget(self.reason_label)

    # --- Signals & Slots ---
    def _connect_signals(self) -> None:
        self.debounce_timer.timeout.connect(self._on_debounce_timer_timeout)

    # --- Styling ---
    def _apply_styles(self) -> None:
        self.setObjectName("lightPanel")
        self.status_label.setObjectName("secondaryLabel")
        self.reason_label.setObjectName("reasonLabel")
        self.reason_label.setWordWrap(True)

    # --- Initial State ---
    def _load_initial_state(self) -> None:
        self.debounce_timer.setSingleShot(True)
        self.debounce_timer.setInterval(DEBOUNCE_MS)
        self.reason_label.setVisible(False)

    # --- Dynamic rows: factory + populate + clear ---
    def _create_light_row_widget(self, channel: LightChannel, level_pct: float) -> LightRowWidget:
        """Factory: ONE light row (name + slider + % value)."""
        return LightRowWidget(channel, level_pct)

    def _connect_light_row_signals(self, row: LightRowWidget) -> None:
        """Wire one generated row (rows do not exist yet in ``_connect_signals``)."""
        row.level_changed.connect(self._on_light_row_level_changed)

    def populate_lights(self, channels: Sequence[Mapping[str, Any]]) -> None:
        """Clear and rebuild the rows from ``TwoDService.list_lights()`` entries."""
        self._clear_light_rows()
        for info in channels:
            channel = LightChannel(
                id=info["id"],
                name=info["name"],
                colour=info.get("colour", ""),
                max_pct=max(float(info["max_pct"]), MIN_CHANNEL_MAX_PCT),
            )
            row = self._create_light_row_widget(channel, float(info["level_pct"]))
            self._connect_light_row_signals(row)
            self._rows[channel.id] = row
            self.rows_layout.addWidget(row)
        self.rows_layout.addStretch(1)
        self.status_label.setText(f"{len(channels)} channels" if channels else NO_LIGHTS_TEXT)

    def _clear_light_rows(self) -> None:
        """Remove all light rows and any pending edit."""
        self.debounce_timer.stop()
        self._pending.clear()
        self._rows.clear()
        remove_all_widgets(self.rows_layout)

    def clear_lights(self) -> None:
        """Public clear."""
        self._clear_light_rows()
        self.status_label.setText(NO_LIGHTS_TEXT)

    # --- Public API ---
    def channel_ids(self) -> list[str]:
        """Channel ids in display order."""
        return list(self._rows)

    def row(self, channel_id: str) -> LightRowWidget | None:
        """The row of a channel."""
        return self._rows.get(channel_id)

    def update_levels(
        self, channels: Sequence[Mapping[str, Any]], rejected: tuple[str, ...] = ()
    ) -> None:
        """Show the read-back levels (rows being dragged or edited are left alone)."""
        for info in channels:
            row = self._rows.get(info["id"])
            if row is not None and not row.is_editing() and info["id"] not in self._pending:
                row.set_level(float(info["level_pct"]))
        self.reason_label.setText("; ".join(rejected))
        self.reason_label.setVisible(bool(rejected))

    # --- Slots ---
    def _on_light_row_level_changed(self, channel_id: str, percent: float) -> None:
        self._pending[channel_id] = percent
        self.debounce_timer.start()

    def _on_debounce_timer_timeout(self) -> None:
        levels, self._pending = self._pending, {}
        if levels:
            self.levels_requested.emit(levels)
