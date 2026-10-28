"""Centre top: the live preview (``QLabel``-based) with a clipped-pixel overlay toggle and the
camera's part area (Phase 13): drawn by dragging on the image, shown dashed when saved."""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, QPointF, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QImage, QMouseEvent, QPainter, QPen, QPixmap, QResizeEvent
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from camera_lab.two_d.gui import styles

MARGIN = 4
SPACING = 6
PREVIEW_MIN_WIDTH = 320
PREVIEW_MIN_HEIGHT = 240
NO_IMAGE_TEXT = "No image - open a camera and press Start live"
DRAW_HINT = "Drag a box around the part on the image"
#: Part area outline: saved (dashed) and being drawn (solid).
AREA_SAVED_COLOUR = "#4fc3f7"
AREA_DRAFT_COLOUR = "#ffd54f"
AREA_PEN_PX = 2
#: Smallest box that can be saved (sensor pixels per side), as in ``two_d.part_area``.
MIN_AREA_SIDE_PX = 8


class LiveViewWidget(QWidget):
    """Shows the newest preview; optionally paints clipped pixels in red on top."""

    overlay_toggled = Signal(bool)
    #: x, y, width, height of the drawn part area in full-sensor pixels
    part_area_save_requested = Signal(int, int, int, int)
    part_area_clear_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._create_widgets()
        self._create_layouts()
        self._connect_signals()
        self._apply_styles()
        self._load_initial_state()

    # --- Widget Construction ---
    def _create_widgets(self) -> None:
        self.image_label = QLabel(NO_IMAGE_TEXT)
        self.overlay_checkbox = QCheckBox("Show clipped pixels")
        self.info_label = QLabel("")
        self.area_checkbox = QCheckBox("Show part area")
        self.draw_area_button = QPushButton("Part area")
        self.save_area_button = QPushButton("Save part area")
        self.cancel_area_button = QPushButton("Cancel")
        self.clear_area_button = QPushButton("Clear part area")
        self.area_label = QLabel("")
        self._image: QImage | None = None
        self._overlay: QImage | None = None
        #: the shown image in sensor pixels: x, y (camera ROI offset), width, height
        self._view: QRect | None = None
        self._saved_area: QRect | None = None  # sensor pixels
        self._draft: QRect | None = None  # sensor pixels, while drawing
        self._drag_from: QPointF | None = None  # sensor pixels
        self._drawing = False
        self._can_draw = False

    # --- Layout ---
    def _create_layouts(self) -> None:
        self.main_layout = QVBoxLayout(self)
        self.footer_layout = QHBoxLayout()
        self.area_layout = QHBoxLayout()

        self.main_layout.setContentsMargins(MARGIN, MARGIN, MARGIN, MARGIN)
        self.main_layout.setSpacing(SPACING)
        self.footer_layout.addWidget(self.overlay_checkbox)
        self.footer_layout.addStretch(1)
        self.footer_layout.addWidget(self.info_label)
        self.area_layout.addWidget(self.draw_area_button)
        self.area_layout.addWidget(self.save_area_button)
        self.area_layout.addWidget(self.cancel_area_button)
        self.area_layout.addWidget(self.clear_area_button)
        self.area_layout.addWidget(self.area_checkbox)
        self.area_layout.addStretch(1)
        self.area_layout.addWidget(self.area_label)
        self.main_layout.addWidget(self.image_label, 1)
        self.main_layout.addLayout(self.area_layout)
        self.main_layout.addLayout(self.footer_layout)

    # --- Signals & Slots ---
    def _connect_signals(self) -> None:
        self.overlay_checkbox.toggled.connect(self._on_overlay_checkbox_toggled)
        self.area_checkbox.toggled.connect(self._on_area_checkbox_toggled)
        self.draw_area_button.clicked.connect(self._on_draw_area_button_clicked)
        self.save_area_button.clicked.connect(self._on_save_area_button_clicked)
        self.cancel_area_button.clicked.connect(self._on_cancel_area_button_clicked)
        self.clear_area_button.clicked.connect(self._on_clear_area_button_clicked)
        self.image_label.installEventFilter(self)

    # --- Styling ---
    def _apply_styles(self) -> None:
        self.setStyleSheet(styles.LIVE_VIEW_STYLE)
        self.image_label.setObjectName("livePreview")
        self.info_label.setObjectName("secondaryLabel")
        self.area_label.setObjectName("secondaryLabel")

    # --- Initial State ---
    def _load_initial_state(self) -> None:
        self.image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image_label.setMinimumSize(PREVIEW_MIN_WIDTH, PREVIEW_MIN_HEIGHT)
        self.image_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self.area_checkbox.setChecked(True)
        self._refresh_area_controls()

    # --- Public API ---
    def show_frame(
        self, image: QImage, overlay: QImage | None = None, view: QRect | None = None
    ) -> None:
        """Show ``image``; ``overlay`` (RGBA, same size) marks the clipped pixels; ``view`` is
        the image in sensor pixels (camera ROI offset and the frame's size)."""
        self._image = image
        self._overlay = overlay
        if view is not None and view != self._view:
            self._view = view
            if self._drawing:  # the camera ROI changed under the drawing: start again
                self._draft = None
                self._drag_from = None
        self._refresh_pixmap()
        self._refresh_area_controls()

    def clear_frame(self) -> None:
        """Forget the image and show the placeholder text."""
        self._image = None
        self._overlay = None
        self._view = None
        self._stop_drawing()
        self.image_label.setPixmap(QPixmap())
        self.image_label.setText(NO_IMAGE_TEXT)

    def set_part_area(self, area: QRect | None) -> None:
        """The camera's saved part area (sensor pixels), or None."""
        self._saved_area = area
        self._stop_drawing()
        self._refresh_pixmap()

    def set_part_area_enabled(self, enabled: bool) -> None:
        """Drawing and saving need an open camera held by this GUI and no job running."""
        self._can_draw = enabled
        if not enabled:
            self._stop_drawing()
        self._refresh_area_controls()

    @property
    def drawing(self) -> bool:
        """True while the part area is being drawn."""
        return self._drawing

    def draft_area(self) -> QRect | None:
        """The box being drawn (sensor pixels)."""
        return self._draft

    def set_info(self, text: str) -> None:
        """Set the line under the preview (size, format, frame id)."""
        self.info_label.setText(text)

    def has_image(self) -> bool:
        """True if a frame is being shown."""
        return self._image is not None

    # --- Helpers ---
    def _compose(self) -> QImage | None:
        if self._image is None:
            return None
        clip = self.overlay_checkbox.isChecked() and self._overlay is not None
        saved = self._saved_area if self.area_checkbox.isChecked() else None
        if not clip and saved is None and self._draft is None:
            return self._image
        composed = self._image.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
        painter = QPainter(composed)
        if clip and self._overlay is not None:
            painter.drawImage(0, 0, self._overlay)
        for rect, colour, style in (
            (saved, AREA_SAVED_COLOUR, Qt.PenStyle.DashLine),
            (self._draft, AREA_DRAFT_COLOUR, Qt.PenStyle.SolidLine),
        ):
            shown = self._to_image(rect) if rect is not None else None
            if shown is not None:
                pen = QPen(QColor(colour), AREA_PEN_PX, style)
                pen.setCosmetic(True)
                painter.setPen(pen)
                painter.drawRect(shown)
        painter.end()
        return composed

    def _to_image(self, rect: QRect) -> QRectF | None:
        """Sensor pixels → preview-image pixels (None when no image or view)."""
        if self._image is None or self._view is None:
            return None
        sx = self._image.width() / self._view.width()
        sy = self._image.height() / self._view.height()
        return QRectF(
            (rect.x() - self._view.x()) * sx,
            (rect.y() - self._view.y()) * sy,
            rect.width() * sx,
            rect.height() * sy,
        )

    def _to_sensor(self, pos: QPointF) -> QPointF | None:
        """A point on the label → sensor pixels, clamped to the image (None off the pixmap)."""
        pixmap = self.image_label.pixmap()
        if self._view is None or pixmap is None or pixmap.isNull():
            return None
        pw, ph = pixmap.width(), pixmap.height()
        left = (self.image_label.width() - pw) / 2
        top = (self.image_label.height() - ph) / 2
        u = min(max((pos.x() - left) / pw, 0.0), 1.0)
        v = min(max((pos.y() - top) / ph, 0.0), 1.0)
        view = self._view
        return QPointF(view.x() + u * view.width(), view.y() + v * view.height())

    def _box(self, a: QPointF, b: QPointF) -> QRect:
        x0, x1 = sorted((round(a.x()), round(b.x())))
        y0, y1 = sorted((round(a.y()), round(b.y())))
        return QRect(x0, y0, x1 - x0, y1 - y0)

    def _describe(self, rect: QRect) -> str:
        text = f"x {rect.x()}, y {rect.y()}, {rect.width()} × {rect.height()} px"
        if self._view is not None:
            pw = 100 * rect.width() / self._view.width()
            ph = 100 * rect.height() / self._view.height()
            text += f" ({pw:.0f} % × {ph:.0f} % of the image)"
        return text

    def _valid_draft(self) -> bool:
        d = self._draft
        return d is not None and min(d.width(), d.height()) >= MIN_AREA_SIDE_PX

    def _stop_drawing(self) -> None:
        self._drawing = False
        self._draft = None
        self._drag_from = None
        self._refresh_area_controls()

    def _refresh_area_controls(self) -> None:
        have_image = self._image is not None and self._view is not None
        self.draw_area_button.setVisible(not self._drawing)
        self.clear_area_button.setVisible(not self._drawing)
        self.save_area_button.setVisible(self._drawing)
        self.cancel_area_button.setVisible(self._drawing)
        self.draw_area_button.setEnabled(self._can_draw and have_image)
        self.clear_area_button.setEnabled(self._can_draw and self._saved_area is not None)
        self.save_area_button.setEnabled(self._drawing and self._valid_draft())
        self.area_checkbox.setEnabled(self._saved_area is not None)
        if self._drawing:
            self.area_label.setText(
                self._describe(self._draft) if self._draft is not None else DRAW_HINT
            )
        elif self._saved_area is not None:
            self.area_label.setText("part area: " + self._describe(self._saved_area))
        else:
            self.area_label.setText("no part area (whole image scored)")

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802 - Qt override
        if watched is not self.image_label or not self._drawing:
            return super().eventFilter(watched, event)
        if not isinstance(event, QMouseEvent):
            return super().eventFilter(watched, event)
        kind = event.type()
        if kind == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
            self._drag_from = self._to_sensor(event.position())
            self._draft = None
        elif kind == QEvent.Type.MouseMove and self._drag_from is not None:
            here = self._to_sensor(event.position())
            if here is not None:
                self._draft = self._box(self._drag_from, here)
        elif kind == QEvent.Type.MouseButtonRelease and self._drag_from is not None:
            here = self._to_sensor(event.position())
            if here is not None:
                self._draft = self._box(self._drag_from, here)
            self._drag_from = None
        else:
            return super().eventFilter(watched, event)
        self._refresh_pixmap()
        self._refresh_area_controls()
        return True

    def _refresh_pixmap(self) -> None:
        composed = self._compose()
        if composed is None:
            return
        size: QSize = self.image_label.size()
        pixmap = QPixmap.fromImage(composed).scaled(
            size,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.FastTransformation,
        )
        self.image_label.setPixmap(pixmap)

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        self._refresh_pixmap()

    # --- Slots ---
    def _on_overlay_checkbox_toggled(self, checked: bool) -> None:
        self._refresh_pixmap()
        self.overlay_toggled.emit(checked)

    def _on_area_checkbox_toggled(self, checked: bool) -> None:
        self._refresh_pixmap()

    def _on_draw_area_button_clicked(self) -> None:
        self._drawing = True
        self._draft = None
        self._refresh_area_controls()

    def _on_save_area_button_clicked(self) -> None:
        draft = self._draft
        if draft is None or not self._valid_draft():
            return
        self._stop_drawing()
        self.part_area_save_requested.emit(draft.x(), draft.y(), draft.width(), draft.height())

    def _on_cancel_area_button_clicked(self) -> None:
        self._stop_drawing()
        self._refresh_pixmap()

    def _on_clear_area_button_clicked(self) -> None:
        self.part_area_clear_requested.emit()
