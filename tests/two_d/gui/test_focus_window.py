"""The Focus window: live view + sharpness meter on the resolution chart."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import cv2
import pytest
from pytestqt.qtbot import QtBot

from camera_lab.two_d.charts import init_builtin
from camera_lab.two_d.gui.main_window import MainWindow
from camera_lab.two_d.service import TwoDService

from ..scene import Scene
from .conftest import SIM, WAIT_MS, open_sim


@pytest.fixture
def scene_service(make_service: Callable[..., TwoDService], tmp_path: Path) -> TwoDService:
    init_builtin(tmp_path / "charts")
    return make_service(charts_root=tmp_path / "charts", backups_root=tmp_path / "backups")


@pytest.fixture
def scene(scene_service: TwoDService, monkeypatch: pytest.MonkeyPatch) -> Scene:
    sc = Scene(scene_service)
    monkeypatch.setattr(scene_service, "grab", sc.grab)
    return sc


@pytest.fixture
def main(qtbot: QtBot, scene_service: TwoDService, scene: Scene) -> Iterator[MainWindow]:
    win = MainWindow(scene_service)
    qtbot.addWidget(win)
    win.show()
    qtbot.waitUntil(lambda: win.camera_list_panel.camera_ids() == [SIM], timeout=WAIT_MS)
    open_sim(qtbot, win)
    yield win
    win.close()


def test_focus_window_shows_sharpness_and_best(
    qtbot: QtBot, main: MainWindow, scene: Scene
) -> None:
    assert main.focus_action.text() == "Focus..." and main.focus_action.isEnabled()
    main.focus_action.trigger()
    dlg = main.focus_window
    assert dlg is not None
    qtbot.waitUntil(lambda: dlg.last_reading() is not None, timeout=WAIT_MS)
    first = dlg.last_reading()
    assert first is not None and first.found and first.main is not None
    assert dlg.readout_text() == f"{first.main:.3f}"
    assert dlg.planes_label.text().startswith("R ") and "best so far" in dlg.best_label.text()
    assert not main.setup_action.isEnabled() and not main.fingerprint_action.isEnabled()
    assert not main.camera_list_panel.disconnect_button.isEnabled()  # one procedure at a time
    scene.page = cv2.GaussianBlur(scene.page, (0, 0), 2.0)  # turned off focus
    qtbot.waitUntil(
        lambda: "past the sharpest point" in dlg.hint_label.text().lower(), timeout=WAIT_MS
    )
    dlg.reset_button.click()
    qtbot.waitUntil(
        lambda: (r := dlg.last_reading()) is not None and r.best_main == r.main, timeout=WAIT_MS
    )
    dlg.close()
    assert main.focus_window is None
    assert main.setup_action.isEnabled() and main.fingerprint_action.isEnabled()
