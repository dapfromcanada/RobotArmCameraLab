"""Centre bottom: the luma histogram, painted with ``QPainter``."""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QPainter, QPaintEvent, QPen, QPolygonF
from PySide6.QtWidgets import QWidget

from camera_lab.two_d.gui import styles

MIN_HEIGHT = 110
PLOT_MARGIN = 6
GRID_DIVISIONS = 4
MEAN_LINE_WIDTH = 2
GRID_LINE_WIDTH = 1
TEXT_OFFSET = 4
TEXT_LINE_HEIGHT = 16
CLIP_BAR_WIDTH = 4
EMPTY_TEXT = "No histogram"


class HistogramWidget(QWidget):
    """Filled luma histogram with a mean marker and a clipped-pixels flag."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._create_widgets()
        self._create_layouts()
        self._connect_signals()
        self._apply_styles()
        self._load_initial_state()

    # --- Widget Construction ---
    def _create_widgets(self) -> None:
        self._bins: tuple[float, ...] = ()
        self._mean = 0.0
        self._clipped_pct = 0.0

    # --- Layout ---
    def _create_layouts(self) -> None:
        """Nothing to arrange: the widget paints itself."""

    # --- Signals & Slots ---
    def _connect_signals(self) -> None:
        """No signals."""

    # --- Styling ---
    def _apply_styles(self) -> None:
        self.setStyleSheet(styles.HISTOGRAM_WIDGET_STYLE)
        self.setAutoFillBackground(True)

    # --- Initial State ---
    def _load_initial_state(self) -> None:
        self.setMinimumHeight(MIN_HEIGHT)

    # --- Public API ---
    def set_histogram(self, bins: Sequence[float], mean: float, clipped_pct: float) -> None:
        """Set the bin fractions (any length), the mean level (0-1) and the clipped %."""
        self._bins = tuple(bins)
        self._mean = mean
        self._clipped_pct = clipped_pct
        self.update()

    def clear_histogram(self) -> None:
        """Remove the histogram."""
        self._bins = ()
        self._mean = 0.0
        self._clipped_pct = 0.0
        self.update()

    def has_data(self) -> bool:
        """True if a histogram is set."""
        return bool(self._bins)

    # --- Painting ---
    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        plot = QRectF(self.rect()).adjusted(PLOT_MARGIN, PLOT_MARGIN, -PLOT_MARGIN, -PLOT_MARGIN)
        painter.fillRect(plot, styles.COLOR_HISTOGRAM_BG)
        self._paint_grid(painter, plot)
        if self._bins:
            self._paint_bins(painter, plot)
            self._paint_mean(painter, plot)
            self._paint_caption(painter, plot)
        else:
            painter.setPen(styles.COLOR_HISTOGRAM_MEAN)
            painter.drawText(plot, Qt.AlignmentFlag.AlignCenter, EMPTY_TEXT)
        painter.end()

    def _paint_grid(self, painter: QPainter, plot: QRectF) -> None:
        painter.setPen(QPen(styles.COLOR_HISTOGRAM_GRID, GRID_LINE_WIDTH))
        for i in range(GRID_DIVISIONS + 1):
            x = plot.left() + plot.width() * i / GRID_DIVISIONS
            painter.drawLine(QPointF(x, plot.top()), QPointF(x, plot.bottom()))

    def _paint_bins(self, painter: QPainter, plot: QRectF) -> None:
        peak = max(self._bins) or 1.0
        n = len(self._bins)
        points = [QPointF(plot.left(), plot.bottom())]
        for i, value in enumerate(self._bins):
            x = plot.left() + plot.width() * (i + 0.5) / n
            points.append(QPointF(x, plot.bottom() - plot.height() * value / peak))
        points.append(QPointF(plot.right(), plot.bottom()))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(styles.COLOR_HISTOGRAM_FILL)
        painter.drawPolygon(QPolygonF(points))
        if self._clipped_pct > 0:
            painter.setBrush(styles.COLOR_HISTOGRAM_CLIP)
            painter.drawRect(
                QRectF(plot.right() - CLIP_BAR_WIDTH, plot.top(), CLIP_BAR_WIDTH, plot.height())
            )

    def _paint_mean(self, painter: QPainter, plot: QRectF) -> None:
        painter.setPen(QPen(styles.COLOR_HISTOGRAM_MEAN, MEAN_LINE_WIDTH))
        x = plot.left() + plot.width() * min(1.0, max(0.0, self._mean))
        painter.drawLine(QPointF(x, plot.top()), QPointF(x, plot.bottom()))

    def _paint_caption(self, painter: QPainter, plot: QRectF) -> None:
        painter.setPen(styles.COLOR_HISTOGRAM_MEAN)
        text = f"mean {self._mean:.3f}   clipped {self._clipped_pct:.2f} %"
        painter.drawText(
            QRectF(plot.left() + TEXT_OFFSET, plot.top(), plot.width(), TEXT_LINE_HEIGHT),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            text,
        )
