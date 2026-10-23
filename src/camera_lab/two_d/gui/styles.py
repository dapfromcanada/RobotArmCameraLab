"""All GUI styling: the dark palette (gui-coding-standards §9) and every stylesheet.

Widgets only call ``setObjectName`` / set dynamic properties; the rules live here.
"""

from __future__ import annotations

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

# ------------------------------------------------------------ §9 palette
DARK_BG = "#2c3e50"
DARK_BG_ALT = "#34495e"
TEXT_PRIMARY = "#ecf0f1"
TEXT_SECONDARY = "#bdc3c7"
ACCENT_BLUE = "#3498db"
ACCENT_GREEN = "#27ae60"
ACCENT_RED = "#e74c3c"
ACCENT_ORANGE = "#f39c12"
BORDER_COLOR = "#1a252f"

# ------------------------------------------------------------ metrics
CONTROL_RADIUS = 4
CONTROL_PADDING_V = 6
CONTROL_PADDING_H = 12
SLIDER_GROOVE_HEIGHT = 6
SLIDER_GROOVE_RADIUS = 3
COMPOSITE_FONT_PT = 20
SLIDER_HANDLE_WIDTH = 14
SLIDER_HANDLE_MARGIN = -5
SLIDER_HANDLE_RADIUS = 7
TAB_PADDING_V = 6
TAB_PADDING_H = 14
SCROLLBAR_WIDTH = 10
FONT_SIZE_PT = 10
SETUP_MESSAGE_FONT_PT = 14
SETUP_MESSAGE_PADDING = 10
SETUP_MESSAGE_BORDER = 2
FP_INSTRUCTION_FONT_PT = 12
FOCUS_READOUT_FONT_PT = 32
FOCUS_BEST_FONT_PT = 16

# ------------------------------------------------------------ colours for painting
COLOR_HISTOGRAM_FILL = QColor(ACCENT_BLUE)
COLOR_HISTOGRAM_GRID = QColor(BORDER_COLOR)
COLOR_HISTOGRAM_MEAN = QColor(ACCENT_ORANGE)
COLOR_HISTOGRAM_CLIP = QColor(ACCENT_RED)
COLOR_HISTOGRAM_BG = QColor(DARK_BG_ALT)
COLOR_CLIP_OVERLAY = QColor(ACCENT_RED)

# ------------------------------------------------------------ stylesheets
GLOBAL_STYLESHEET = f"""
QWidget {{
    background-color: {DARK_BG};
    color: {TEXT_PRIMARY};
    font-size: {FONT_SIZE_PT}pt;
}}
QMainWindow, QDialog {{ background-color: {DARK_BG}; }}
QToolBar {{
    background-color: {DARK_BG_ALT};
    border-bottom: 1px solid {BORDER_COLOR};
    spacing: 4px;
}}
QToolButton {{
    background-color: {DARK_BG_ALT};
    color: {TEXT_PRIMARY};
    border: 1px solid {BORDER_COLOR};
    border-radius: {CONTROL_RADIUS}px;
    padding: {CONTROL_PADDING_V}px {CONTROL_PADDING_H}px;
}}
QToolButton:hover {{ background-color: {ACCENT_BLUE}; }}
QToolButton:checked {{ background-color: {ACCENT_GREEN}; }}
QToolButton:disabled {{ color: {TEXT_SECONDARY}; background-color: {DARK_BG}; }}
QPushButton {{
    background-color: {ACCENT_BLUE};
    color: {TEXT_PRIMARY};
    border: none;
    border-radius: {CONTROL_RADIUS}px;
    padding: {CONTROL_PADDING_V}px {CONTROL_PADDING_H}px;
}}
QPushButton:hover {{ background-color: {ACCENT_BLUE}; border: 1px solid {TEXT_PRIMARY}; }}
QPushButton:disabled {{ background-color: {BORDER_COLOR}; color: {TEXT_SECONDARY}; }}
QPushButton:checked {{ background-color: {ACCENT_GREEN}; }}
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QListWidget, QPlainTextEdit {{
    background-color: {DARK_BG_ALT};
    color: {TEXT_PRIMARY};
    border: 1px solid {BORDER_COLOR};
    border-radius: {CONTROL_RADIUS}px;
    padding: 2px 4px;
}}
QComboBox QAbstractItemView {{
    background-color: {DARK_BG_ALT};
    color: {TEXT_PRIMARY};
    selection-background-color: {ACCENT_BLUE};
}}
QSlider::groove:horizontal {{
    height: {SLIDER_GROOVE_HEIGHT}px;
    background: {BORDER_COLOR};
    border-radius: {SLIDER_GROOVE_RADIUS}px;
}}
QSlider::sub-page:horizontal {{
    background: {ACCENT_BLUE};
    border-radius: {SLIDER_GROOVE_RADIUS}px;
}}
QSlider::handle:horizontal {{
    background: {TEXT_PRIMARY};
    width: {SLIDER_HANDLE_WIDTH}px;
    margin: {SLIDER_HANDLE_MARGIN}px 0;
    border-radius: {SLIDER_HANDLE_RADIUS}px;
}}
QTabWidget::pane {{ border: 1px solid {BORDER_COLOR}; }}
QTabBar::tab {{
    background-color: {DARK_BG_ALT};
    color: {TEXT_SECONDARY};
    padding: {TAB_PADDING_V}px {TAB_PADDING_H}px;
    border: 1px solid {BORDER_COLOR};
}}
QTabBar::tab:selected {{ background-color: {ACCENT_BLUE}; color: {TEXT_PRIMARY}; }}
QStatusBar {{ background-color: {DARK_BG_ALT}; border-top: 1px solid {BORDER_COLOR}; }}
QStatusBar QLabel {{ background-color: transparent; padding: 0 8px; }}
QScrollArea {{ border: none; }}
QScrollBar:vertical {{ background: {DARK_BG}; width: {SCROLLBAR_WIDTH}px; }}
QScrollBar::handle:vertical {{ background: {BORDER_COLOR}; border-radius: {CONTROL_RADIUS}px; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QFrame#cameraRow, QFrame#runRow, QFrame#captureThumb, QFrame#scoreRow,
QFrame#processorRow, QFrame#settingRow, QFrame#lightRow {{
    background-color: {DARK_BG_ALT};
    border: 1px solid {BORDER_COLOR};
    border-radius: {CONTROL_RADIUS}px;
}}
QFrame#cameraRow QLabel, QFrame#runRow QLabel, QFrame#captureThumb QLabel,
QFrame#scoreRow QLabel, QFrame#processorRow QLabel, QFrame#settingRow QLabel,
QFrame#lightRow QLabel {{ background-color: transparent; border: none; }}
QPushButton#cameraButton, QPushButton#captureThumbButton {{
    background-color: {DARK_BG_ALT};
    text-align: left;
    border: 1px solid {BORDER_COLOR};
}}
QPushButton#cameraButton:checked, QPushButton#captureThumbButton:checked {{
    background-color: {ACCENT_BLUE};
}}
QLabel#reasonLabel {{ color: {ACCENT_RED}; background-color: transparent; }}
QLabel#reasonLabel[level="warning"] {{ color: {ACCENT_ORANGE}; }}
QLabel#secondaryLabel, QLabel#unitLabel {{ color: {TEXT_SECONDARY}; }}
QLabel#warningLabel {{ color: {ACCENT_ORANGE}; }}
QLabel#compositeLabel {{ font-size: {COMPOSITE_FONT_PT}pt; color: {ACCENT_BLUE}; }}
QLabel#passMarker {{ color: {ACCENT_GREEN}; background-color: transparent; }}
QLabel#failMarker {{ color: {ACCENT_RED}; background-color: transparent; }}
QFrame#setupRow {{
    background-color: {DARK_BG_ALT};
    border: 1px solid {BORDER_COLOR};
    border-radius: {CONTROL_RADIUS}px;
}}
QFrame#setupRow QLabel {{ background-color: transparent; border: none; }}
QLabel#setupStatus {{ color: {TEXT_SECONDARY}; font-weight: bold; }}
QLabel#setupStatus[state="running"] {{ color: {ACCENT_BLUE}; }}
QLabel#setupStatus[state="pass"] {{ color: {ACCENT_GREEN}; }}
QLabel#setupStatus[state="skipped"] {{ color: {TEXT_SECONDARY}; }}
QLabel#setupStatus[state="stop"] {{ color: {ACCENT_RED}; }}
QLabel#setupMessage {{
    background-color: {DARK_BG_ALT};
    border: {SETUP_MESSAGE_BORDER}px solid {BORDER_COLOR};
    border-radius: {CONTROL_RADIUS}px;
    padding: {SETUP_MESSAGE_PADDING}px;
    font-size: {SETUP_MESSAGE_FONT_PT}pt;
}}
QLabel#setupMessage[level="stop"] {{
    border-color: {ACCENT_RED};
    color: {ACCENT_RED};
    font-weight: bold;
}}
QLabel#setupMessage[level="pass"] {{ border-color: {ACCENT_GREEN}; }}
QLabel#fpInstruction {{
    background-color: {DARK_BG_ALT};
    border: {SETUP_MESSAGE_BORDER}px solid {ACCENT_BLUE};
    border-radius: {CONTROL_RADIUS}px;
    padding: {SETUP_MESSAGE_PADDING}px;
    font-size: {FP_INSTRUCTION_FONT_PT}pt;
}}
QFrame#fpResultRow {{
    background-color: {DARK_BG_ALT};
    border: 1px solid {BORDER_COLOR};
}}
QFrame#fpResultRow QLabel {{ background-color: transparent; border: none; }}
QLabel#fpResultKey {{ color: {TEXT_SECONDARY}; }}
QLabel#fpResultValue {{ color: {ACCENT_GREEN}; font-weight: bold; }}
QLabel#focusReadout {{ font-size: {FOCUS_READOUT_FONT_PT}pt; color: {ACCENT_BLUE}; }}
QLabel#focusBest {{ font-size: {FOCUS_BEST_FONT_PT}pt; color: {ACCENT_GREEN}; }}
QLabel#livePreview {{ background-color: {BORDER_COLOR}; border: 1px solid {BORDER_COLOR}; }}
QLabel#comparePreview {{ background-color: {BORDER_COLOR}; }}
"""

#: Per-widget sheets (set in each widget's ``_apply_styles``).
HISTOGRAM_WIDGET_STYLE = f"HistogramWidget {{ background-color: {DARK_BG_ALT}; }}"
LIVE_VIEW_STYLE = f"LiveViewWidget {{ background-color: {DARK_BG}; }}"


def build_dark_palette() -> QPalette:
    """Return the §9 dark palette as a ``QPalette``."""
    pal = QPalette()
    pal.setColor(QPalette.ColorRole.Window, QColor(DARK_BG))
    pal.setColor(QPalette.ColorRole.WindowText, QColor(TEXT_PRIMARY))
    pal.setColor(QPalette.ColorRole.Base, QColor(DARK_BG_ALT))
    pal.setColor(QPalette.ColorRole.AlternateBase, QColor(DARK_BG))
    pal.setColor(QPalette.ColorRole.Text, QColor(TEXT_PRIMARY))
    pal.setColor(QPalette.ColorRole.Button, QColor(DARK_BG_ALT))
    pal.setColor(QPalette.ColorRole.ButtonText, QColor(TEXT_PRIMARY))
    pal.setColor(QPalette.ColorRole.ToolTipBase, QColor(DARK_BG_ALT))
    pal.setColor(QPalette.ColorRole.ToolTipText, QColor(TEXT_PRIMARY))
    pal.setColor(QPalette.ColorRole.PlaceholderText, QColor(TEXT_SECONDARY))
    pal.setColor(QPalette.ColorRole.Highlight, QColor(ACCENT_BLUE))
    pal.setColor(QPalette.ColorRole.HighlightedText, QColor(TEXT_PRIMARY))
    pal.setColor(QPalette.ColorRole.Link, QColor(ACCENT_BLUE))
    pal.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor(TEXT_SECONDARY))
    pal.setColor(
        QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor(TEXT_SECONDARY)
    )
    return pal


def apply_dark_theme(app: QApplication) -> None:
    """Apply the Fusion style, the dark palette and the global stylesheet to ``app``."""
    app.setStyle("Fusion")
    app.setPalette(build_dark_palette())
    app.setStyleSheet(GLOBAL_STYLESHEET)
