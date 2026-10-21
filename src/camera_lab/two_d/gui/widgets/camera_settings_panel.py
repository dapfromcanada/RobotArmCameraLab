"""Camera tab: a profile Mode combo and one control row per setting the camera reports.

Rows are generated from the open camera's effective ranges and options
(``SettingsView``). Settings a camera does not have (e.g. plain ``exposure_us``
in the Triton's HDR mode) simply get no row. Edits are coalesced for
``DEBOUNCE_MS`` and sent as one :class:`CameraSettingsPatch`.
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError
from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from camera_lab.core.safety import EffectiveRange, NotAdjustable
from camera_lab.two_d.gui.constants import DEBOUNCE_MS
from camera_lab.two_d.gui.widgets.layout_utils import remove_all_widgets
from camera_lab.two_d.gui.widgets.setting_row_widget import SettingRowWidget
from camera_lab.two_d.models import (
    ROI,
    ApplyResult,
    CameraSettings,
    CameraSettingsPatch,
    FieldResult,
    SettingSpec,
)
from camera_lab.two_d.service import SettingsView

MARGIN = 8
SPACING = 6
CORE_ORDER = (
    "exposure_us",
    "gain_db",
    "gamma",
    "wb_red",
    "wb_green",
    "wb_blue",
    "frame_rate_hz",
    "pixel_format",
    "trigger_mode",
    "roi_x",
    "roi_y",
    "roi_width",
    "roi_height",
)
INT_SETTINGS = frozenset({"roi_x", "roi_y", "roi_width", "roi_height"})
SIMPLE_FIELDS = ("exposure_us", "gain_db", "gamma", "frame_rate_hz")
CHOICE_FIELDS = ("pixel_format", "trigger_mode")
WB_PREFIX = "wb_"
ROI_PREFIX = "roi_"
UNIT_BY_SUFFIX = {"_us": "us", "_db": "dB", "_hz": "Hz"}
PIXEL_UNIT = "px"
NO_CAMERA_TEXT = "No camera open"


def unit_for(name: str) -> str:
    """Unit text for a setting name (the service view carries no units)."""
    if name.startswith(ROI_PREFIX):
        return PIXEL_UNIT
    for suffix, unit in UNIT_BY_SUFFIX.items():
        if name.endswith(suffix):
            return unit
    return ""


def current_value(settings: CameraSettings, name: str) -> float | str | None:
    """Read the current value of setting ``name`` from the settings read-back."""
    value: Any
    if name in SIMPLE_FIELDS or name in CHOICE_FIELDS:
        value = getattr(settings, name)
    elif name.startswith(WB_PREFIX):
        value = (settings.white_balance or {}).get(name[len(WB_PREFIX) :])  # type: ignore[call-overload]
    elif name.startswith(ROI_PREFIX):
        value = getattr(settings.roi, name[len(ROI_PREFIX) :], None)
    else:
        value = settings.advanced.get(name)
    return value if isinstance(value, int | float | str) else None


def sort_key(name: str) -> tuple[int, int, str]:
    """Core settings first (fixed order), then advanced ones alphabetically."""
    if name in CORE_ORDER:
        return (0, CORE_ORDER.index(name), name)
    return (1, 0, name)


def specs_from_view(view: SettingsView) -> tuple[list[SettingSpec], list[str]]:
    """Build one ``SettingSpec`` per adjustable setting + notes for non-adjustable ones."""
    specs: list[SettingSpec] = []
    notes: list[str] = []
    flags = {
        "affects_calibration": set(view.affects_calibration),
        "requires_stream_stop": set(view.requires_stream_stop),
    }
    for name, rng in view.ranges.items():
        if isinstance(rng, NotAdjustable):
            notes.append(f"{name}: not adjustable ({rng.reason})")
            continue
        assert isinstance(rng, EffectiveRange)  # noqa: S101 - exhaustive union
        specs.append(
            SettingSpec(
                name=name,
                kind="int" if name in INT_SETTINGS else "float",
                min=rng.min.value,
                max=rng.max.value,
                increment=rng.increment,
                unit=unit_for(name),
                affects_calibration=name in flags["affects_calibration"],
                requires_stream_stop=name in flags["requires_stream_stop"],
            )
        )
    for name, options in view.options.items():
        if not options:
            continue
        specs.append(
            SettingSpec(
                name=name,
                kind="enum",
                options=tuple(options),
                affects_calibration=name in flags["affects_calibration"],
                requires_stream_stop=name in flags["requires_stream_stop"],
            )
        )
    specs.sort(key=lambda s: sort_key(s.name))
    return specs, notes


def _first_error(exc: ValueError) -> str:
    """The first validation message of ``exc`` (pydantic errors are a ValueError too)."""
    if isinstance(exc, ValidationError):
        return str(exc.errors()[0]["msg"])
    return str(exc)


def row_name_for_field(field: str) -> list[str]:
    """Row names a ``FieldResult.field`` belongs to."""
    if field == "roi":
        return [f"{ROI_PREFIX}{p}" for p in ("x", "y", "width", "height")]
    if field.startswith("white_balance."):
        return [f"{WB_PREFIX}{field.split('.', 1)[1]}"]
    if field.startswith("advanced."):
        return [field.split(".", 1)[1]]
    return [field]


class CameraSettingsPanel(QWidget):
    """Mode combo + generated setting rows. Emits patches; never talks to the service."""

    patch_requested = Signal(object)
    mode_requested = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._create_widgets()
        self._create_layouts()
        self._connect_signals()
        self._apply_styles()
        self._load_initial_state()

    # --- Widget Construction ---
    def _create_widgets(self) -> None:
        self.mode_label = QLabel("Mode")
        self.mode_combo = QComboBox()
        self.status_label = QLabel(NO_CAMERA_TEXT)
        self.rows_container = QWidget()
        self.rows_scroll = QScrollArea()
        self.notes_label = QLabel("")
        self.debounce_timer = QTimer(self)
        self._rows: dict[str, SettingRowWidget] = {}
        self._pending: dict[str, Any] = {}
        self._view: SettingsView | None = None

    # --- Layout ---
    def _create_layouts(self) -> None:
        self.main_layout = QVBoxLayout(self)
        self.mode_layout = QHBoxLayout()
        self.rows_layout = QVBoxLayout(self.rows_container)

        self.main_layout.setContentsMargins(MARGIN, MARGIN, MARGIN, MARGIN)
        self.main_layout.setSpacing(SPACING)
        self.mode_layout.addWidget(self.mode_label)
        self.mode_layout.addWidget(self.mode_combo, 1)
        self.rows_layout.setContentsMargins(0, 0, 0, 0)
        self.rows_layout.setSpacing(SPACING)
        self.rows_scroll.setWidget(self.rows_container)
        self.rows_scroll.setWidgetResizable(True)
        self.main_layout.addLayout(self.mode_layout)
        self.main_layout.addWidget(self.status_label)
        self.main_layout.addWidget(self.rows_scroll, 1)
        self.main_layout.addWidget(self.notes_label)

    # --- Signals & Slots ---
    def _connect_signals(self) -> None:
        self.mode_combo.activated.connect(self._on_mode_combo_activated)
        self.debounce_timer.timeout.connect(self._on_debounce_timer_timeout)

    # --- Styling ---
    def _apply_styles(self) -> None:
        self.setObjectName("cameraSettingsPanel")
        self.status_label.setObjectName("secondaryLabel")
        self.notes_label.setObjectName("warningLabel")
        self.notes_label.setWordWrap(True)

    # --- Initial State ---
    def _load_initial_state(self) -> None:
        self.debounce_timer.setSingleShot(True)
        self.debounce_timer.setInterval(DEBOUNCE_MS)
        self.mode_combo.setEnabled(False)
        self.notes_label.setVisible(False)

    # --- Dynamic rows: factory + populate + clear ---
    def _create_setting_row_widget(
        self, spec: SettingSpec, value: float | str | None = None
    ) -> SettingRowWidget:
        """Factory: ONE setting row for ``spec`` (a slider/spinbox row or an enum combo)."""
        row = SettingRowWidget(spec, value)
        row.setObjectName("settingRow")
        return row

    def _connect_setting_row_signals(self, row: SettingRowWidget) -> None:
        """Wire one generated row (rows do not exist yet in ``_connect_signals``)."""
        row.value_changed.connect(self._on_setting_row_value_changed)

    def populate_settings(self, view: SettingsView) -> None:
        """Clear and rebuild every row from ``view``."""
        self._clear_setting_rows()
        self._view = view
        specs, notes = specs_from_view(view)
        for spec in specs:
            row = self._create_setting_row_widget(spec, current_value(view.settings, spec.name))
            self._connect_setting_row_signals(row)
            self._rows[spec.name] = row
            self.rows_layout.addWidget(row)
        self.rows_layout.addStretch(1)
        self.notes_label.setText("\n".join(notes))
        self.notes_label.setVisible(bool(notes))
        self.status_label.setText(f"{len(specs)} settings")

    def _clear_setting_rows(self) -> None:
        """Remove all setting rows (and any edit still waiting to be sent)."""
        self.debounce_timer.stop()
        self._pending.clear()
        self._rows.clear()
        remove_all_widgets(self.rows_layout)

    def clear_settings(self) -> None:
        """Public clear: no camera open."""
        self._clear_setting_rows()
        self._view = None
        self.notes_label.setVisible(False)
        self.status_label.setText(NO_CAMERA_TEXT)
        self.clear_modes()

    # --- Public API ---
    def row_names(self) -> list[str]:
        """Names of the generated rows, in display order."""
        return list(self._rows)

    def row(self, name: str) -> SettingRowWidget | None:
        """The row for setting ``name`` (``None`` if the camera has no such setting)."""
        return self._rows.get(name)

    def show_view(self, view: SettingsView) -> None:
        """Adopt a fresh read-back: rebuild if the set of settings changed, else update in place."""
        specs, notes = specs_from_view(view)
        if [s.name for s in specs] != list(self._rows):
            self.populate_settings(view)
            return
        self._view = view
        for spec in specs:
            row = self._rows[spec.name]
            if row.spec != spec:
                row.update_spec(spec)
            value = current_value(view.settings, spec.name)
            if value is not None and not row.is_editing() and spec.name not in self._pending:
                row.set_value(value)
        self.notes_label.setText("\n".join(notes))
        self.notes_label.setVisible(bool(notes))

    def populate_modes(
        self, modes: tuple[str, ...], current: str | None, default: str | None
    ) -> None:
        """Fill the Mode combo; select the entered mode, else the profile default."""
        self.mode_combo.blockSignals(True)
        self.mode_combo.clear()
        self.mode_combo.addItems(list(modes))
        shown = current or default
        if shown is not None:
            self.mode_combo.setCurrentText(shown)
        self.mode_combo.blockSignals(False)
        self.mode_combo.setEnabled(bool(modes))

    def clear_modes(self) -> None:
        """Empty the Mode combo."""
        self.mode_combo.blockSignals(True)
        self.mode_combo.clear()
        self.mode_combo.blockSignals(False)
        self.mode_combo.setEnabled(False)

    def show_mode(self, mode: str | None) -> None:
        """Select ``mode`` in the combo without emitting."""
        if mode is None:
            return
        self.mode_combo.blockSignals(True)
        self.mode_combo.setCurrentText(mode)
        self.mode_combo.blockSignals(False)

    def apply_result(self, result: ApplyResult) -> None:
        """Show per-row outcomes (rejection/clamp reasons) and the read-back values."""
        for row in self._rows.values():
            if row.name not in self._pending:
                row.clear_reason()
        for fr in result.results:
            self._show_field_result(fr)

    def show_error(self, message: str) -> None:
        """Show a service-level error (e.g. a step budget) in the status line."""
        self.status_label.setText(message)

    # --- Helpers ---
    def _show_field_result(self, fr: FieldResult) -> None:
        for row_name in row_name_for_field(fr.field):
            row = self._rows.get(row_name)
            if row is not None:
                row.show_result(fr.status, fr.reason)

    def _build_patch(self, changes: dict[str, Any]) -> CameraSettingsPatch:
        kwargs: dict[str, Any] = {}
        wb: dict[str, float] = {}
        roi_parts: dict[str, int] = {}
        advanced: dict[str, float | str] = {}
        for name, value in changes.items():
            if name in SIMPLE_FIELDS:
                kwargs[name] = float(value)
            elif name in CHOICE_FIELDS:
                kwargs[name] = str(value)
            elif name.startswith(WB_PREFIX):
                wb[name[len(WB_PREFIX) :]] = float(value)
            elif name.startswith(ROI_PREFIX):
                roi_parts[name[len(ROI_PREFIX) :]] = round(float(value))
            else:
                advanced[name] = value
        if wb:
            kwargs["white_balance"] = wb
        if advanced:
            kwargs["advanced"] = advanced
        if roi_parts:
            base = self._view.settings.roi.model_dump() if self._view is not None else {}
            kwargs["roi"] = ROI(**{**base, **roi_parts})
        return CameraSettingsPatch(**kwargs)

    # --- Slots ---
    def _on_setting_row_value_changed(self, name: str, value: object) -> None:
        self._pending[name] = value
        row = self._rows.get(name)
        if row is not None:
            row.clear_reason()
        self.debounce_timer.start()

    def _on_debounce_timer_timeout(self) -> None:
        changes, self._pending = self._pending, {}
        if not changes:
            return
        try:
            patch = self._build_patch(changes)
        except (ValidationError, ValueError) as exc:
            for name in changes:
                row = self._rows.get(name)
                if row is not None:
                    row.show_result(
                        "rejected",
                        _first_error(exc),
                    )
            return
        self.patch_requested.emit(patch)

    def _on_mode_combo_activated(self, index: int) -> None:
        mode = self.mode_combo.itemText(index)
        if mode:
            self.mode_requested.emit(mode)
