"""The "Camera fingerprint" window on the simulated sensor (offscreen)."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
import yaml
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QFrame
from pytestqt.qtbot import QtBot

from camera_lab.two_d.drivers.simulated import SensorModel, SimulatedCameraDriver
from camera_lab.two_d.fingerprint.session import FP_SETUPS, FpSetup
from camera_lab.two_d.gui.main_window import MainWindow
from camera_lab.two_d.gui.widgets.fingerprint_window import FingerprintWindow
from camera_lab.two_d.models import CameraSettingsPatch
from camera_lab.two_d.service import TwoDService

from .conftest import SIM, WAIT_MS, open_sim

SENSOR = SensorModel(dark_current_e_s=200, prnu=0.01, vignetting=0.3, hot_pixels=20)
STALL_LIMIT_MS = 200.0
RUN_MS = 180_000  # one Run makes several measurements, the darks and flats 3× each


@pytest.fixture
def fp_service(make_service: Callable[..., TwoDService], tmp_path: Path) -> TwoDService:
    drv = SimulatedCameraDriver(None, sensor=SENSOR, black_level=0.05)
    return make_service(drivers={"simulated": drv}, backups_root=tmp_path / "backups")


def set_scene(svc: TwoDService, kind: str) -> None:
    svc._open[SIM].camera.scene = kind  # type: ignore[attr-defined]  # the human's action


@pytest.fixture
def main(qtbot: QtBot, fp_service: TwoDService, tmp_path: Path) -> Iterator[MainWindow]:
    win = MainWindow(fp_service)
    qtbot.addWidget(win)
    win.worker.fingerprint_options = {"root": tmp_path / "fp", "reset": False}  # sim set up below
    win.show()
    qtbot.waitUntil(lambda: win.camera_list_panel.camera_ids() == [SIM], timeout=WAIT_MS)
    open_sim(qtbot, win)
    fp_service.apply(SIM, CameraSettingsPatch(pixel_format="Mono8"))  # mono: Mono12 is raw
    yield win
    win.close()


def open_fp(qtbot: QtBot, main: MainWindow) -> FingerprintWindow:
    assert main.fingerprint_action.isEnabled()
    main.fingerprint_action.trigger()
    dlg = main.fingerprint_window
    assert dlg is not None
    qtbot.waitUntil(lambda: bool(dlg.checklist()), timeout=WAIT_MS)
    return dlg


def pick_lens(qtbot: QtBot, dlg: FingerprintWindow, name: str = "Kowa LM8HC") -> None:
    """The human picks the lens on the camera from the list (never typed)."""
    index = next(i for i, text in enumerate(dlg.lens_items()) if text.startswith(name))
    dlg._on_lens_combo_activated(index)
    qtbot.waitUntil(lambda: dlg._lens_id is not None and not dlg._busy, timeout=WAIT_MS)


def press(qtbot: QtBot, dlg: FingerprintWindow) -> None:
    """Tick the checklist (the human confirms) and press Run step / Retry."""
    if dlg._lens_id is None:
        pick_lens(qtbot, dlg)
    dlg.tick_all()
    button = dlg.retry_button if dlg.failed_step is not None else dlg.run_step_button
    qtbot.waitUntil(button.isEnabled, timeout=WAIT_MS)
    button.click()


def run_step(qtbot: QtBot, dlg: FingerprintWindow, step: FpSetup, state: str = "pass") -> None:
    assert dlg.next_step == step or dlg.failed_step == step
    press(qtbot, dlg)
    qtbot.waitUntil(lambda: dlg.row_states()[step] == state, timeout=RUN_MS)
    if dlg.next_step is not None:
        qtbot.waitUntil(
            lambda: not dlg._busy,
            timeout=WAIT_MS,
        )


def test_action_needs_an_open_camera(qtbot: QtBot, fp_service: TwoDService) -> None:
    win = MainWindow(fp_service)
    qtbot.addWidget(win)
    win.show()
    assert win.fingerprint_action.text() == "Camera fingerprint..."
    assert not win.fingerprint_action.isEnabled()
    win.close()


#: Before a lens is picked (sim: HDR-capable, no recipe → discovery; the lens's stops are not
#: known yet, so no stop rows)
ROWS = ["cap", "diffuser", "light_2", "low_light", "finish", "power"]


def test_window_opens_with_live_frames(qtbot: QtBot, main: MainWindow) -> None:
    dlg = open_fp(qtbot, main)
    qtbot.waitUntil(dlg.live_view.has_image, timeout=WAIT_MS)
    assert len(FP_SETUPS) == 16
    assert dlg.step_row_count() == 6  # unused setups get no row
    assert dlg.row_states() == dict.fromkeys(ROWS, "pending")
    assert not dlg.retry_button.isEnabled()
    assert not dlg.skip_button.isEnabled()


def test_run_needs_every_checklist_item(qtbot: QtBot, main: MainWindow) -> None:
    dlg = open_fp(qtbot, main)
    items = dlg.checklist()
    assert "Iris fully open" in items and "Lens cap on" in items
    assert "Focus: not needed" in items
    assert dlg.lens_items()[0] == "pick the lens on the camera"  # two lenses: nothing assumed
    dlg.tick_all()
    assert not dlg.run_step_button.isEnabled()  # no lens picked
    pick_lens(qtbot, dlg)
    assert "Iris at f/1.4 (fully open)" in dlg.checklist()  # the checklist names the stop
    assert "Kowa LM8HC, f/1.4" in dlg.lens_text_label.text()
    assert not dlg.run_step_button.isEnabled()  # a new checklist after the pick
    boxes = dlg._check_boxes
    for box in boxes[:-1]:
        box.setChecked(True)
    assert not dlg.run_step_button.isEnabled()  # one item left
    boxes[-1].setChecked(True)
    assert dlg.run_step_button.isEnabled()


def test_instruction_changes_per_setup(
    qtbot: QtBot, main: MainWindow, fp_service: TwoDService
) -> None:
    dlg = open_fp(qtbot, main)
    assert "LENS CAP" in dlg.instruction_text()
    set_scene(fp_service, "cap")
    run_step(qtbot, dlg, "cap")
    assert "OPAL DIFFUSER" in dlg.instruction_text()
    assert dlg.instruction_text() == dlg._plan["diffuser"].instruction
    assert "Lens cap off" in dlg.checklist()
    assert "3 runs agree within" in dlg.row_message("cap")


def test_cap_and_diffuser_stop_then_pass_after_the_human_fixes_it(
    qtbot: QtBot, main: MainWindow, fp_service: TwoDService
) -> None:
    dlg = open_fp(qtbot, main)
    press(qtbot, dlg)  # scene is the chart: the cap is not on
    qtbot.waitUntil(lambda: dlg.row_states()["cap"] == "STOP", timeout=WAIT_MS)
    qtbot.waitUntil(lambda: bool(dlg.checklist()) and not dlg._busy, timeout=WAIT_MS)
    assert not dlg.retry_button.isEnabled()  # the checklist must be confirmed again
    assert not dlg.skip_button.isEnabled()  # the cap setup cannot be skipped
    dlg.tick_all()
    qtbot.waitUntil(dlg.retry_button.isEnabled, timeout=WAIT_MS)
    assert "lens cap" in dlg.message_text()
    assert dlg.message_text().startswith("STOP - darks:")
    assert dlg.message_label.property("level") == "stop"
    assert dlg.failed_step == "cap"
    assert not dlg.run_step_button.isEnabled()
    set_scene(fp_service, "cap")
    run_step(qtbot, dlg, "cap")
    assert dlg.failed_step is None

    # diffuser: still capped, then the chart: both stop
    press(qtbot, dlg)
    qtbot.waitUntil(lambda: dlg.row_states()["diffuser"] == "STOP", timeout=RUN_MS)
    qtbot.waitUntil(lambda: not dlg._busy, timeout=WAIT_MS)
    set_scene(fp_service, "chart")
    press(qtbot, dlg)
    qtbot.waitUntil(
        lambda: dlg.row_states()["diffuser"] == "STOP" and not dlg._busy, timeout=RUN_MS
    )
    assert "diffuser" in dlg.message_text()
    set_scene(fp_service, "flat")
    run_step(qtbot, dlg, "diffuser")
    assert "system gain" in dlg.row_message("diffuser")


def test_full_run_reaches_the_report_without_a_ui_stall(
    qtbot: QtBot, main: MainWindow, fp_service: TwoDService, tmp_path: Path
) -> None:
    dlg = open_fp(qtbot, main)
    main.heartbeat.reset()
    pick_lens(qtbot, dlg)
    set_scene(fp_service, "cap")
    run_step(qtbot, dlg, "cap")
    set_scene(fp_service, "flat")
    run_step(qtbot, dlg, "diffuser")
    run_step(qtbot, dlg, "light_2", "skipped")  # not an HDR camera
    cam = fp_service._open[SIM].camera
    assert not dlg.lens_combo.isEnabled()  # fixed once a row has run
    session = main.worker._fingerprint
    assert session is not None
    stop_rows = [s for s, p in session.plan.items() if p.active and s.startswith("lens_")]
    assert len(stop_rows) == 6  # the Kowa's marks between f/1.4 and f/16
    for setup in (*stop_rows, "low_light"):  # the human turns the iris mark by mark
        cam.iris_transmission = session._expected_at(session.plan[setup].f_number)  # type: ignore[attr-defined, arg-type]
        run_step(qtbot, dlg, setup)
    cam.iris_transmission = 1.0  # type: ignore[attr-defined]
    run_step(qtbot, dlg, "finish")
    fp_service._open[SIM].camera.power_cut()  # type: ignore[attr-defined]  # unplugged
    run_step(qtbot, dlg, "power")
    qtbot.waitUntil(lambda: dlg.next_step is None, timeout=WAIT_MS)
    texts = "\n".join(dlg.result_texts())
    for key in ("System gain", "Read noise", "Full well", "Dark current", "Hot pixels", "PRNU"):
        assert key in texts
    assert "Vignetting" in texts
    assert "gain dB" in texts  # the noise-vs-gain table
    assert dlg.result_row_count() > 10
    folder = dlg.folder_label.text()
    assert str(tmp_path / "fp") in folder
    doc = yaml.safe_load((Path(folder.removeprefix("Folder: ")) / "fingerprint.yaml").read_text())
    assert doc["identity"]["lens"] == "Kowa LM8HC, f/1.4"
    assert "complete" in dlg.message_text().lower()
    dlg.copy_button.click()
    from PySide6.QtGui import QGuiApplication

    copied = QGuiApplication.clipboard().text()
    assert "3. HDR channels (discovery) [skipped]: HDR: not an HDR camera" in copied
    assert "System gain" in copied and "Folder:" in copied
    assert not dlg.run_step_button.isEnabled()
    assert main.worker.frames_emitted > 0
    assert main.max_ui_stall_ms < STALL_LIMIT_MS, main.max_ui_stall_ms


def test_step_rows_populate_and_clear_without_leftovers(qtbot: QtBot, main: MainWindow) -> None:
    dlg = open_fp(qtbot, main)
    for _ in range(3):
        dlg._populate_step_list()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    rows = [w for w in dlg.steps_container.findChildren(QFrame) if w.objectName() == "setupRow"]
    assert len(rows) == 6
    assert dlg.step_row_count() == 6
    dlg._clear_step_list()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert dlg.step_row_count() == 0
    assert dlg.steps_container.findChildren(QFrame) == []
    assert dlg.row_states() == {}
    dlg._populate_results([("a", "1")], [("x", "y", "z")])
    dlg._populate_results([("b", "2")], [])
    dlg._clear_results()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert dlg.results_container.findChildren(QFrame) == []


def test_closing_the_window_keeps_the_camera_open(
    qtbot: QtBot, main: MainWindow, fp_service: TwoDService
) -> None:
    dlg = open_fp(qtbot, main)
    qtbot.waitUntil(dlg.live_view.has_image, timeout=WAIT_MS)
    dlg.close()
    assert main.fingerprint_window is None
    assert main.camera_id == SIM
    assert fp_service.get_settings(SIM) is not None
    seen = main.worker.frames_emitted
    qtbot.waitUntil(lambda: main.worker.frames_emitted > seen + 2, timeout=WAIT_MS)
    assert main.fingerprint_action.isEnabled()
    again = open_fp(qtbot, main)
    assert again is not dlg
    assert again.row_states() == dict.fromkeys(ROWS, "pending")


def test_add_a_lens_puts_it_in_the_catalog_and_picks_it(
    qtbot: QtBot, main: MainWindow, monkeypatch: pytest.MonkeyPatch
) -> None:
    from PySide6.QtWidgets import QDialog

    from camera_lab.two_d.catalog import Catalog
    from camera_lab.two_d.gui.widgets.add_lens_dialog import AddLensDialog

    dlg = open_fp(qtbot, main)
    add = AddLensDialog()
    qtbot.addWidget(add)
    ok = add.buttons.button(add.buttons.StandardButton.Ok)
    assert not ok.isEnabled()  # name and two stops needed
    add.name_edit.setText("  Fuji 12 mm   HF12XA ")
    add.stop_boxes[1.6 if 1.6 in add.stop_boxes else 2.0].setChecked(True)
    assert not ok.isEnabled()
    add.stop_boxes[16.0].setChecked(True)
    assert ok.isEnabled()
    assert add.values() == ("Fuji 12 mm HF12XA", 8.0, "C", (2.0, 16.0))

    def fake_exec(self: AddLensDialog) -> int:
        self.name_edit.setText("Fuji 12 mm HF12XA")
        self.focal_spin.setValue(12.0)
        for f in (2.0, 2.8, 4.0, 5.6, 8.0, 11.0, 16.0):
            self.stop_boxes[f].setChecked(True)
        return int(QDialog.DialogCode.Accepted.value)

    monkeypatch.setattr(AddLensDialog, "exec", fake_exec)
    dlg._on_add_lens_button_clicked()
    qtbot.waitUntil(lambda: dlg._lens_id == "fuji_12mm_hf12xa" and not dlg._busy, timeout=WAIT_MS)
    assert any(t.startswith("Fuji 12 mm HF12XA") for t in dlg.lens_items())
    assert "Iris at f/2 (fully open)" in dlg.checklist()
    with Catalog() as cat:
        lens = cat.lens("fuji_12mm_hf12xa")
        assert lens is not None and (lens.open_f, lens.closed_f, lens.focal_mm) == (2.0, 16.0, 12.0)
        assert any(c["actor"] == "human" and c["tbl"] == "lenses" for c in cat.changes(20))
