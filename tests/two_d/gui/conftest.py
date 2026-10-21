"""Fixtures for GUI tests: offscreen Qt, a MainWindow on the simulated camera + light.

Sim only: the real Arena/eBUS drivers are never loaded (tests/conftest.py).
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from typing import Any

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pytestqt.qtbot import QtBot

from camera_lab.two_d.gui.main_window import MainWindow
from camera_lab.two_d.service import TwoDService

SIM = "SIM-0001"
WAIT_MS = 15_000


class Spy:
    """Records the calls of a service method while still running the real one."""

    def __init__(self, svc: TwoDService, name: str) -> None:
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        original = getattr(svc, name)

        def wrapper(*args: Any, **kwargs: Any) -> Any:
            self.calls.append((args, kwargs))
            return original(*args, **kwargs)

        setattr(svc, name, wrapper)


@pytest.fixture
def spy() -> Callable[[TwoDService, str], Spy]:
    return Spy


@pytest.fixture
def gui_service(make_service: Callable[..., TwoDService]) -> TwoDService:
    """A sim service whose camera is NOT open yet (the GUI opens it)."""
    return make_service()


@pytest.fixture
def window(qtbot: QtBot, gui_service: TwoDService) -> Iterator[MainWindow]:
    win = MainWindow(gui_service)
    qtbot.addWidget(win)
    win.show()
    qtbot.waitUntil(lambda: win.camera_list_panel.camera_ids() == [SIM], timeout=WAIT_MS)
    yield win
    win.close()


@pytest.fixture
def open_window(qtbot: QtBot, window: MainWindow) -> MainWindow:
    """The window with SIM-0001 opened through the camera list."""
    open_sim(qtbot, window)
    return window


def open_sim(qtbot: QtBot, window: MainWindow) -> None:
    window.camera_list_panel._buttons[SIM].click()
    qtbot.waitUntil(
        lambda: (
            window.camera_id == SIM and "exposure_us" in window.camera_settings_panel.row_names()
        ),
        timeout=WAIT_MS,
    )
