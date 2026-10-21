"""GUI entry point used by ``camera-lab gui``."""

from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

from camera_lab.core.lights import install_lights_off_handlers
from camera_lab.core.logging import get_logger
from camera_lab.two_d.gui import styles
from camera_lab.two_d.gui.main_window import MainWindow
from camera_lab.two_d.service import TwoDService, build_service

_log = get_logger(__name__)


def run_gui(sim: bool = False, service: TwoDService | None = None) -> int:
    """Build the service (unless given), show the main window, return the exit code."""
    app = QApplication.instance() or QApplication(sys.argv[:1])
    assert isinstance(app, QApplication)  # noqa: S101 - narrowing for mypy
    styles.apply_dark_theme(app)
    svc = service or build_service(sim=sim)
    uninstall = (
        install_lights_off_handlers(svc.light_service.controller)
        if svc.light_service is not None
        else None
    )
    window = MainWindow(svc)
    window.show()
    try:
        code = app.exec()
    finally:
        window.close()  # idempotent: stops workers, closes cameras, lights off
        svc.shutdown()
        if uninstall is not None:
            uninstall()
    _log.info("gui_exit", code=code, max_ui_stall_ms=round(window.max_ui_stall_ms, 1))
    return int(code)
