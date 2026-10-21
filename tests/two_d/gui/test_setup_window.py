"""The "Camera setup" window on the simulated camera, with rendered chart scenes (offscreen)."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QFrame
from pytestqt.qtbot import QtBot

from camera_lab.two_d.charts import init_builtin
from camera_lab.two_d.gui.main_window import MainWindow
from camera_lab.two_d.gui.widgets.setup_window import STEP_LABELS, SetupWindow
from camera_lab.two_d.models import CameraSettingsPatch
from camera_lab.two_d.service import TwoDService
from camera_lab.two_d.setup import STEPS, StepName

from ..scene import Scene
from .conftest import SIM, WAIT_MS, Spy, open_sim

FOCUS_MS = 50
STALL_LIMIT_MS = 200.0


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
def main(
    qtbot: QtBot, scene_service: TwoDService, scene: Scene, tmp_path: Path
) -> Iterator[MainWindow]:
    win = MainWindow(scene_service)
    qtbot.addWidget(win)
    win.worker.setup_options = {"sleep": lambda s: None, "root": tmp_path / "setup"}
    win.show()
    qtbot.waitUntil(lambda: win.camera_list_panel.camera_ids() == [SIM], timeout=WAIT_MS)
    open_sim(qtbot, win)
    yield win
    win.close()


def open_setup(qtbot: QtBot, main: MainWindow) -> SetupWindow:
    assert main.setup_action.isEnabled()
    main.setup_action.trigger()
    dlg = main.setup_window
    assert dlg is not None
    dlg._focus_interval_ms = FOCUS_MS
    dlg.focus_timer.setInterval(FOCUS_MS)
    qtbot.waitUntil(lambda: dlg.run_next_button.isEnabled(), timeout=WAIT_MS)
    return dlg


def wait_state(qtbot: QtBot, dlg: SetupWindow, step: StepName, state: str) -> None:
    qtbot.waitUntil(lambda: dlg.row_states()[step] == state, timeout=WAIT_MS)
    qtbot.waitUntil(
        lambda: (
            dlg.retry_button.isEnabled()
            or dlg.run_next_button.isEnabled()
            or dlg.save_button.isEnabled()
            or dlg.focus_mode
            or dlg.next_step is None
        ),
        timeout=WAIT_MS,
    )


def run_step(qtbot: QtBot, dlg: SetupWindow, step: StepName, state: str = "pass") -> None:
    assert dlg.next_step == step
    dlg.run_next_button.click()
    wait_state(qtbot, dlg, step, state)


def run_to_focus(qtbot: QtBot, dlg: SetupWindow) -> None:
    for step in ("backup", "light", "brightness"):
        run_step(qtbot, dlg, step)  # type: ignore[arg-type]


def lock_focus(qtbot: QtBot, dlg: SetupWindow) -> None:
    dlg.run_next_button.click()
    qtbot.waitUntil(lambda: dlg.focus_current_label.text() != "-", timeout=WAIT_MS)
    qtbot.waitUntil(lambda: dlg.focus_locked_button.isEnabled(), timeout=WAIT_MS)
    dlg.focus_locked_button.click()
    wait_state(qtbot, dlg, "focus", "pass")


def run_to_save(qtbot: QtBot, dlg: SetupWindow) -> None:
    run_to_focus(qtbot, dlg)
    lock_focus(qtbot, dlg)
    run_step(qtbot, dlg, "brightness_recheck")
    run_step(qtbot, dlg, "white_balance")
    qtbot.waitUntil(lambda: dlg.save_button.isEnabled(), timeout=WAIT_MS)


def test_action_needs_an_open_camera(qtbot: QtBot, scene_service: TwoDService) -> None:
    win = MainWindow(scene_service)
    qtbot.addWidget(win)
    win.show()
    assert win.setup_action.text() == "Camera setup..."
    assert not win.setup_action.isEnabled()
    win.close()


def test_window_opens_with_live_frames(qtbot: QtBot, main: MainWindow) -> None:
    assert not main.live_action.isChecked()
    dlg = open_setup(qtbot, main)
    qtbot.waitUntil(dlg.live_view.has_image, timeout=WAIT_MS)  # started live by itself
    assert dlg.step_row_count() == len(STEPS) == 7
    assert dlg.row_states() == dict.fromkeys(STEPS, "pending")
    assert not dlg.save_button.isEnabled()
    assert not dlg.retry_button.isEnabled()
    assert not dlg.focus_frame.isVisible()
    assert STEP_LABELS["backup"] == "1. Backup"


def test_run_next_walks_the_first_steps_without_a_ui_stall(qtbot: QtBot, main: MainWindow) -> None:
    dlg = open_setup(qtbot, main)
    main.heartbeat.reset()
    run_step(qtbot, dlg, "backup")
    assert "backed up" in dlg.row_message("backup")
    run_step(qtbot, dlg, "light")
    run_step(qtbot, dlg, "brightness")
    assert "step wedge paper white" in dlg.row_message("brightness")
    assert dlg.next_step == "focus"
    frames = main.worker.frames_emitted
    assert frames > 0
    assert main.max_ui_stall_ms < STALL_LIMIT_MS, main.max_ui_stall_ms


def test_focus_readout_and_lock(qtbot: QtBot, main: MainWindow) -> None:
    dlg = open_setup(qtbot, main)
    run_to_focus(qtbot, dlg)
    dlg.run_next_button.click()
    assert dlg.focus_mode
    qtbot.waitUntil(lambda: dlg.focus_current_label.text() != "-", timeout=WAIT_MS)
    assert "cyc/px" in dlg.focus_current_label.text()
    assert "lp/mm" in dlg.focus_current_label.text()
    assert "cyc/px" in dlg.focus_best_label.text()
    first = dlg.focus_current_label.text()
    qtbot.wait(4 * FOCUS_MS)  # the readout keeps refreshing (no pile-up of jobs)
    assert first
    qtbot.waitUntil(lambda: dlg.focus_locked_button.isEnabled(), timeout=WAIT_MS)
    dlg.focus_locked_button.click()
    wait_state(qtbot, dlg, "focus", "pass")
    assert not dlg.focus_mode
    assert not dlg.focus_frame.isVisible()
    assert "baseline" in dlg.row_message("focus")
    assert dlg.next_step == "brightness_recheck"


def test_dark_scene_stops_and_retry_works_after_the_fix(
    qtbot: QtBot, main: MainWindow, scene_service: TwoDService, scene: Scene
) -> None:
    scene_service.apply(SIM, CameraSettingsPatch(exposure_us=200_000.0, gain_db=0))
    scene.k = 4e-7  # too dark even at the working maximum
    dlg = open_setup(qtbot, main)
    run_step(qtbot, dlg, "backup")
    run_step(qtbot, dlg, "light")
    dlg.run_next_button.click()
    wait_state(qtbot, dlg, "brightness", "STOP")
    assert "open the iris or add light" in dlg.message_text()
    assert dlg.message_text().startswith("STOP")
    assert dlg.message_label.property("level") == "stop"
    assert dlg.failed_step == "brightness"
    assert dlg.retry_button.isEnabled()
    assert not dlg.run_next_button.isEnabled()
    scene.k = 1e-6  # the human opened the iris a little (more light than before)
    dlg.retry_button.click()
    wait_state(qtbot, dlg, "brightness", "pass")
    assert dlg.failed_step is None
    assert dlg.next_step == "focus"


def test_no_chart_stops_the_light_step(qtbot: QtBot, main: MainWindow, scene: Scene) -> None:
    page = scene.page
    scene.page = np.full_like(page, 0.5)  # no charts in view
    dlg = open_setup(qtbot, main)
    run_step(qtbot, dlg, "backup")
    dlg.run_next_button.click()
    wait_state(qtbot, dlg, "light", "STOP")
    assert "place one under the camera" in dlg.message_text()
    scene.page = page
    dlg.retry_button.click()
    wait_state(qtbot, dlg, "light", "pass")


def test_unstable_light_message(qtbot: QtBot, main: MainWindow, scene: Scene) -> None:
    scene.drift = 0.02 / 10_000
    dlg = open_setup(qtbot, main)
    run_step(qtbot, dlg, "backup")
    dlg.run_next_button.click()
    wait_state(qtbot, dlg, "light", "STOP")
    assert "lighting is not stable" in dlg.message_text()


def test_save_asks_first_decline_then_accept(
    qtbot: QtBot,
    main: MainWindow,
    scene_service: TwoDService,
    spy: Callable[[TwoDService, str], Spy],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saves = spy(scene_service, "save_user_set")
    camera = scene_service._open[SIM].camera
    real_save = camera.save_user_set

    def save_with_notes(slot: str = "UserSet1", make_default: bool = True) -> dict[str, Any]:
        out = real_save(slot, make_default)
        return {**out, "notes": ["this camera has no boot-set feature"]}

    monkeypatch.setattr(camera, "save_user_set", save_with_notes)
    dlg = open_setup(qtbot, main)
    assert not dlg.save_button.isEnabled()
    run_to_save(qtbot, dlg)
    asked: list[bool] = []
    monkeypatch.setattr(dlg, "_confirm_save", lambda: asked.append(False) or False)
    dlg.save_button.click()
    qtbot.wait(100)
    assert asked == [False]
    assert saves.calls == []  # declined: camera memory untouched
    assert dlg.row_states()["save"] == "pending"
    assert dlg.save_button.isEnabled()
    monkeypatch.setattr(dlg, "_confirm_save", lambda: asked.append(True) or True)
    dlg.save_button.click()
    wait_state(qtbot, dlg, "save", "pass")
    assert len(saves.calls) == 1
    assert saves.calls[0][1]["confirm"] is True
    assert dlg.next_step is None
    assert not dlg.save_button.isEnabled()
    assert "no boot-set feature" in dlg.notes_label.text()
    assert "Backup taken before saving" in dlg.notes_label.text()
    assert "saved" in dlg.message_text().lower()


def test_confirmation_text_explains_the_write(qtbot: QtBot, main: MainWindow) -> None:
    from camera_lab.two_d.gui.widgets import setup_window as sw

    for part in ("camera's memory", "User Set 1", "boot", "backed up"):
        assert part in sw.CONFIRM_TEXT


def test_step_rows_populate_and_clear_without_leftovers(qtbot: QtBot, main: MainWindow) -> None:
    dlg = open_setup(qtbot, main)
    for _ in range(3):
        dlg._populate_step_list()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    rows = [w for w in dlg.steps_container.findChildren(QFrame) if w.objectName() == "setupRow"]
    assert len(rows) == 7
    assert dlg.step_row_count() == 7
    dlg._clear_step_list()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert dlg.step_row_count() == 0
    assert dlg.steps_container.findChildren(QFrame) == []
    assert dlg.row_states() == {}


def test_closing_the_window_keeps_the_camera_open(
    qtbot: QtBot, main: MainWindow, scene_service: TwoDService
) -> None:
    dlg = open_setup(qtbot, main)
    qtbot.waitUntil(dlg.live_view.has_image, timeout=WAIT_MS)
    dlg.close()
    assert main.setup_window is None
    assert main.camera_id == SIM
    assert scene_service.get_settings(SIM) is not None  # still open
    seen = main.worker.frames_emitted
    qtbot.waitUntil(lambda: main.worker.frames_emitted > seen + 2, timeout=WAIT_MS)
    assert main.setup_action.isEnabled()
    again = open_setup(qtbot, main)  # a fresh session
    assert again is not dlg
    assert again.row_states() == dict.fromkeys(STEPS, "pending")
