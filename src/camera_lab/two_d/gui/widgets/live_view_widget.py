"""Centre top: the live preview (``QLabel``-based) with a clipped-pixel overlay toggle."""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QImage, QPainter, QPixmap, QResizeEvent
from PySide6.QtWidgets import QCheckBox, QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget

from camera_lab.two_d.gui import styles

MARGIN = 4
SPACING = 6
PREVIEW_MIN_WIDTH = 320
PREVIEW_MIN_HEIGHT = 240
NO_IMAGE_TEXT = "No image - open a camera and press Start live"


class LiveViewWidget(QWidget):
    """Shows the newest preview; optionally paints clipped pixels in red on top."""

    overlay_toggled = Signal(bool)

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
        self._image: QImage | None = None
        self._overlay: QImage | None = None

    # --- Layout ---
    def _create_layouts(self) -> None:
        self.main_layout = QVBoxLayout(self)
        self.footer_layout = QHBoxLayout()

        self.main_layout.setContentsMargins(MARGIN, MARGIN, MARGIN, MARGIN)
        self.main_layout.setSpacing(SPACING)
        self.footer_layout.addWidget(self.overlay_checkbox)
        self.footer_layout.addStretch(1)
        self.footer_layout.addWidget(self.info_label)
        self.main_layout.addWidget(self.image_label, 1)
        self.main_layout.addLayout(self.footer_layout)

    # --- Signals & Slots ---
    def _connect_signals(self) -> None:
        self.overlay_checkbox.toggled.connect(self._on_overlay_checkbox_toggled)

    # --- Styling ---
    def _apply_styles(self) -> None:
        self.setStyleSheet(styles.LIVE_VIEW_STYLE)
        self.image_label.setObjectName("livePreview")
        self.info_label.setObjectName("secondaryLabel")

    # --- Initial State ---
    def _load_initial_state(self) -> None:
        self.image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image_label.setMinimumSize(PREVIEW_MIN_WIDTH, PREVIEW_MIN_HEIGHT)
        self.image_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)

    # --- Public API ---
    def show_frame(self, image: QImage, overlay: QImage | None = None) -> None:
        """Show ``image``; ``overlay`` (RGBA, same size) marks the clipped pixels."""
        self._image = image
        self._overlay = overlay
        self._refresh_pixmap()

    def clear_frame(self) -> None:
        """Forget the image and show the placeholder text."""
        self._image = None
        self._overlay = None
        self.image_label.setPixmap(QPixmap())
        self.image_label.setText(NO_IMAGE_TEXT)

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
        if not self.overlay_checkbox.isChecked() or self._overlay is None:
            return self._image
        composed = self._image.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
        painter = QPainter(composed)
        painter.drawImage(0, 0, self._overlay)
        painter.end()
        return composed

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
