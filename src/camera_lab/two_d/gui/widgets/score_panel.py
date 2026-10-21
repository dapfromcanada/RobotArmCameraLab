"""Scores tab: composite score, metric rows, live fast scores and processor output rows."""

from __future__ import annotations

from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QScrollArea, QVBoxLayout, QWidget

from camera_lab.two_d.gui.widgets.layout_utils import remove_all_widgets
from camera_lab.two_d.models import MetricResult, ProcessorResult, ScoreReport

MARGIN = 8
SPACING = 6
ROW_MARGIN = 6
NAME_WIDTH = 140
MARKER_WIDTH = 20
OK_MARK = "OK"
FAIL_MARK = "FAIL"
NA_MARK = "-"
NO_SCORES_TEXT = "-"


def format_metric_value(metric: MetricResult) -> str:
    """``0.4523`` or ``0.31 %`` text for a metric."""
    unit = f" {metric.unit}" if metric.unit else ""
    return f"{metric.value:.4g}{unit}"


class ScorePanel(QWidget):
    """Metric rows (name + value + pass/fail), composite, processors."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._create_widgets()
        self._create_layouts()
        self._connect_signals()
        self._apply_styles()
        self._load_initial_state()

    # --- Widget Construction ---
    def _create_widgets(self) -> None:
        self.composite_title_label = QLabel("Composite score")
        self.composite_label = QLabel(NO_SCORES_TEXT)
        self.live_label = QLabel("Live: -")
        self.metrics_title_label = QLabel("Metrics")
        self.metrics_container = QWidget()
        self.metrics_scroll = QScrollArea()
        self.processors_title_label = QLabel("Processors")
        self.processors_container = QWidget()

    # --- Layout ---
    def _create_layouts(self) -> None:
        self.main_layout = QVBoxLayout(self)
        self.metrics_layout = QVBoxLayout(self.metrics_container)
        self.processors_layout = QVBoxLayout(self.processors_container)

        self.main_layout.setContentsMargins(MARGIN, MARGIN, MARGIN, MARGIN)
        self.main_layout.setSpacing(SPACING)
        for layout in (self.metrics_layout, self.processors_layout):
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(SPACING)
        self.metrics_scroll.setWidget(self.metrics_container)
        self.metrics_scroll.setWidgetResizable(True)
        self.main_layout.addWidget(self.composite_title_label)
        self.main_layout.addWidget(self.composite_label)
        self.main_layout.addWidget(self.live_label)
        self.main_layout.addWidget(self.metrics_title_label)
        self.main_layout.addWidget(self.metrics_scroll, 1)
        self.main_layout.addWidget(self.processors_title_label)
        self.main_layout.addWidget(self.processors_container)

    # --- Signals & Slots ---
    def _connect_signals(self) -> None:
        """No signals: the panel only displays."""

    # --- Styling ---
    def _apply_styles(self) -> None:
        self.setObjectName("scorePanel")
        self.composite_label.setObjectName("compositeLabel")
        self.live_label.setObjectName("secondaryLabel")
        self.composite_title_label.setObjectName("secondaryLabel")

    # --- Initial State ---
    def _load_initial_state(self) -> None:
        self.processors_title_label.setVisible(False)

    # --- Dynamic rows: factory + populate + clear ---
    def _create_score_row_widget(self, metric: MetricResult) -> QFrame:
        """Factory: ONE metric row (name + value + pass/fail marker)."""
        row = QFrame()
        row.setObjectName("scoreRow")
        name_label = QLabel(metric.name)
        name_label.setMinimumWidth(NAME_WIDTH)
        value_label = QLabel(format_metric_value(metric))
        marker_label = QLabel(
            NA_MARK if metric.passed is None else (OK_MARK if metric.passed else FAIL_MARK)
        )
        marker_label.setObjectName(
            "passMarker"
            if metric.passed
            else ("failMarker" if metric.passed is False else "unitLabel")
        )
        marker_label.setMinimumWidth(MARKER_WIDTH)
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(ROW_MARGIN, ROW_MARGIN, ROW_MARGIN, ROW_MARGIN)
        row_layout.addWidget(name_label)
        row_layout.addWidget(value_label, 1)
        row_layout.addWidget(marker_label)
        return row

    def populate_scores(self, report: ScoreReport) -> None:
        """Show a full score report: composite + one row per metric."""
        self._clear_score_rows()
        self.composite_label.setText(f"{report.composite:.1f}")
        for metric in report.metrics:
            self.metrics_layout.addWidget(self._create_score_row_widget(metric))
        self.metrics_layout.addStretch(1)

    def _clear_score_rows(self) -> None:
        """Remove all metric rows."""
        remove_all_widgets(self.metrics_layout)

    def clear_scores(self) -> None:
        """Public clear."""
        self._clear_score_rows()
        self.composite_label.setText(NO_SCORES_TEXT)

    def _create_processor_row_widget(self, result: ProcessorResult) -> QFrame:
        """Factory: ONE processor output row (processor name + result text)."""
        row = QFrame()
        row.setObjectName("processorRow")
        name_label = QLabel(result.name)
        name_label.setMinimumWidth(NAME_WIDTH)
        text = ", ".join(f"{k}={v}" for k, v in result.result.items()) or "-"
        result_label = QLabel(text)
        result_label.setWordWrap(True)
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(ROW_MARGIN, ROW_MARGIN, ROW_MARGIN, ROW_MARGIN)
        row_layout.addWidget(name_label)
        row_layout.addWidget(result_label, 1)
        return row

    def populate_processors(self, results: list[ProcessorResult]) -> None:
        """Show the plugin/processor output rows."""
        self._clear_processor_rows()
        for result in results:
            self.processors_layout.addWidget(self._create_processor_row_widget(result))
        self.processors_title_label.setVisible(bool(results))

    def _clear_processor_rows(self) -> None:
        """Remove all processor rows."""
        remove_all_widgets(self.processors_layout)

    def clear_processors(self) -> None:
        """Public clear."""
        self._clear_processor_rows()
        self.processors_title_label.setVisible(False)

    # --- Public API ---
    def set_live_scores(self, mean: float, clipped_pct: float, fps: float) -> None:
        """Show the fast preview scores."""
        self.live_label.setText(
            f"Live: mean {mean:.3f}   clipped {clipped_pct:.2f} %   {fps:.1f} fps"
        )
