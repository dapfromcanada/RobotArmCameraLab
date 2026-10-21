"""MainWindow behaviour on the simulated camera (offscreen)."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from PySide6.QtWidgets import QApplication
from pytestqt.qtbot import QtBot

from camera_lab.core.locks import CameraLock
from camera_lab.core.logging import get_logger
from camera_lab.two_d.gui import styles
from camera_lab.two_d.gui.main_window import MainWindow
from camera_lab.two_d.service import TwoDService

from .conftest import SIM, WAIT_MS, Spy, open_sim


def test_window_opens_with_sim(window: MainWindow) -> None:
    assert window.isVisible()
    assert window.windowTitle() == "camera_lab"
    tabs = [window.right_tabs.tabText(i) for i in range(window.right_tabs.count())]
    assert tabs == ["Camera", "Light", "Scores", "Runs"]
    actions = [
        a.text() for a in (window.live_action, window.capture_action, window.tune_action)
    ] + [a.text() for a in (window.save_preset_action, window.apply_preset_action)]
    assert actions == [
        "Start live",
        "Capture",
        "Run classic tune",
        "Save preset...",
        "Apply preset...",
    ]
    assert window.lights_off_action.text() == "All lights off"
    assert not window.capture_action.isEnabled()  # no camera open yet
    assert window.state_label.text() == "no camera"


def test_camera_list_populates_and_rescan(qtbot: QtBot, window: MainWindow) -> None:
    assert window.camera_list_panel.camera_ids() == [SIM]
    window.camera_list_panel.rescan_button.click()
    qtbot.waitUntil(lambda: window.status_bar.currentMessage() == "1 camera(s) found")


def test_open_camera_builds_settings_lights_modes(qtbot: QtBot, window: MainWindow) -> None:
    open_sim(qtbot, window)
    names = window.camera_settings_panel.row_names()
    for expected in (
        "exposure_us",
        "gain_db",
        "pixel_format",
        "trigger_mode",
        "hdr.ch0.exposure_us",
        "hdr.enabled",
        "prism.exposure_mode",
    ):
        assert expected in names
    combo = window.camera_settings_panel.mode_combo
    assert [combo.itemText(i) for i in range(combo.count())] == ["linear", "hdr", "prism"]
    assert window.lock_label.text() == f"lock: gui ({SIM})"
    qtbot.waitUntil(lambda: window.light_panel.channel_ids() != [], timeout=WAIT_MS)
    assert "white_ring" in window.light_panel.channel_ids()
    assert window.capture_action.isEnabled()


def test_exposure_slider_calls_apply_with_the_right_value(
    qtbot: QtBot, open_window: MainWindow, gui_service: TwoDService
) -> None:
    spy = Spy(gui_service, "apply")
    row = open_window.camera_settings_panel.row("exposure_us")
    assert row is not None
    # a burst of slider moves is coalesced into a single apply with the last value
    for pos in (100, 200, 300, 400, 500):
        row.value_slider.setValue(pos)
    expected = row.value_from_slider(500)
    qtbot.waitUntil(lambda: len(spy.calls) >= 1, timeout=WAIT_MS)
    qtbot.wait(400)
    assert len(spy.calls) == 1
    args, _ = spy.calls[0]
    assert args[0] == SIM
    patch = args[1]
    assert patch.exposure_us == expected
    assert patch.changed_fields() == ["exposure_us"]
    assert args[2] == "human"
    qtbot.waitUntil(
        lambda: gui_service.get_settings(SIM).settings.exposure_us == expected, timeout=WAIT_MS
    )


def test_out_of_limit_typed_value_shows_the_rejection_reason(
    qtbot: QtBot, open_window: MainWindow, gui_service: TwoDService
) -> None:
    row = open_window.camera_settings_panel.row("exposure_us")
    assert row is not None
    before = row.value_spinbox.value()
    row.value_spinbox.setValue(9_999_999.0)  # far above the camera's 1 s maximum
    qtbot.waitUntil(lambda: row.reason_text().startswith("rejected"), timeout=WAIT_MS)
    assert "exposure_us" in row.reason_text()
    assert "camera max" in row.reason_text()
    assert row.reason_label.isVisibleTo(open_window)
    qtbot.waitUntil(lambda: row.value_spinbox.value() == before, timeout=WAIT_MS)  # reverted
    assert gui_service.get_settings(SIM).settings.exposure_us == before


def test_roi_edit_sends_a_complete_roi(
    qtbot: QtBot, open_window: MainWindow, gui_service: TwoDService
) -> None:
    spy = Spy(gui_service, "apply")
    row = open_window.camera_settings_panel.row("roi_x")
    assert row is not None
    row.value_spinbox.setValue(16)
    qtbot.waitUntil(lambda: len(spy.calls) == 1, timeout=WAIT_MS)
    patch = spy.calls[0][0][1]
    assert patch.roi.x == 16
    assert patch.roi.width == 1024


def test_mode_combo_enters_the_mode(
    qtbot: QtBot, open_window: MainWindow, gui_service: TwoDService
) -> None:
    combo = open_window.camera_settings_panel.mode_combo
    index = combo.findText("prism")
    combo.setCurrentIndex(index)
    combo.activated.emit(index)
    qtbot.waitUntil(lambda: gui_service.current_mode(SIM) == "prism", timeout=WAIT_MS)


def test_capture_shows_scores_runs_and_compare(qtbot: QtBot, open_window: MainWindow) -> None:
    win = open_window
    win.capture_action.trigger()
    qtbot.waitUntil(lambda: win.score_panel.composite_label.text() != "-", timeout=WAIT_MS)
    assert win.score_panel.metrics_layout.count() > 5
    qtbot.waitUntil(lambda: win.score_panel.processors_layout.count() >= 1, timeout=WAIT_MS)
    qtbot.waitUntil(lambda: win.capture_action.isEnabled(), timeout=WAIT_MS)
    win.capture_action.trigger()
    qtbot.waitUntil(lambda: len(win._capture_choices) == 2, timeout=WAIT_MS)
    runs = win.run_browser_panel
    qtbot.waitUntil(lambda: len(runs.run_ids()) == 2, timeout=WAIT_MS)  # one manual run each
    runs._run_buttons[runs.run_ids()[0]].click()
    qtbot.waitUntil(lambda: runs.capture_ids() != [], timeout=WAIT_MS)
    # compare two captures side by side (from two runs: select, then another run)
    first = runs.capture_ids()[0]
    runs.select_capture(first)
    assert runs.compared_ids() == [first]
    assert runs.compare_caption_a.text().startswith("A: ")
    assert not runs.compare_image_a.pixmap().isNull()
    runs._run_buttons[runs.run_ids()[1]].click()
    qtbot.waitUntil(
        lambda: runs.capture_ids() != [] and runs.capture_ids()[0] != first, timeout=WAIT_MS
    )
    assert runs.compared_ids() == []  # the capture list was rebuilt


def test_compare_keeps_at_most_two(qtbot: QtBot, open_window: MainWindow) -> None:
    win = open_window
    runs = win.run_browser_panel
    start = win.camera_settings_panel.row("exposure_us")
    assert start is not None
    start.value_spinbox.setValue(100.0)  # far too dark: the tuner needs several captures
    qtbot.waitUntil(lambda: win.service.get_settings(SIM).settings.exposure_us == 100.0)
    win.tune_action.trigger()  # one run with several captures, recorded on the worker thread
    qtbot.waitUntil(lambda: win.warning_label.text().startswith("Tune"), timeout=60_000)
    qtbot.waitUntil(lambda: len(runs.run_ids()) == 1, timeout=WAIT_MS)
    runs._run_buttons[runs.run_ids()[0]].click()
    try:
        qtbot.waitUntil(lambda: len(runs.capture_ids()) >= 3, timeout=WAIT_MS)
    except Exception:
        raise AssertionError(win.warning_label.text()) from None
    ids = runs.capture_ids()
    for cid in ids[:3]:
        runs.select_capture(cid)
    assert runs.compared_ids() == ids[1:3]
    assert runs.compare_title_label.text() == "Comparing A and B"
    assert runs.compare_caption_a.text().startswith("A: ")
    assert runs.compare_caption_b.text().startswith("B: ")
    assert not runs.compare_image_b.pixmap().isNull()
    assert not runs._capture_buttons[ids[0]].isChecked()  # the oldest selection was dropped
    assert runs.selected_capture_id() == ids[2]


def test_preset_save_and_apply(
    qtbot: QtBot, open_window: MainWindow, gui_service: TwoDService
) -> None:
    win = open_window
    win.capture_action.trigger()
    qtbot.waitUntil(lambda: len(win._capture_choices) == 1, timeout=WAIT_MS)

    def accept_save(dialog: Any) -> bool:
        assert dialog.mode == "save"
        dialog.name_input.setText("bench-a")
        return True

    win._exec_dialog = accept_save  # type: ignore[method-assign]
    win.save_preset_action.trigger()
    qtbot.waitUntil(lambda: win.warning_label.text().startswith("Preset saved"), timeout=WAIT_MS)
    assert [p.name for p in gui_service.list_presets(SIM)] == ["bench-a"]

    # change exposure, then apply the preset back
    row = win.camera_settings_panel.row("exposure_us")
    assert row is not None
    saved = gui_service.get_settings(SIM).settings.exposure_us
    row.value_spinbox.setValue(saved + 1000)
    qtbot.waitUntil(lambda: gui_service.get_settings(SIM).settings.exposure_us == saved + 1000)

    def accept_apply(dialog: Any) -> bool:
        assert dialog.mode == "apply"
        assert dialog.choice_value() == "bench-a"
        return True

    win._exec_dialog = accept_apply  # type: ignore[method-assign]
    win.apply_preset_action.trigger()
    qtbot.waitUntil(
        lambda: gui_service.get_settings(SIM).settings.exposure_us == saved, timeout=WAIT_MS
    )


def test_save_preset_without_capture_warns(open_window: MainWindow) -> None:
    open_window.save_preset_action.trigger()
    assert "capture an image first" in open_window.warning_label.text()


def test_lights_slider_and_all_off(
    qtbot: QtBot, open_window: MainWindow, gui_service: TwoDService
) -> None:
    win = open_window
    qtbot.waitUntil(lambda: win.light_panel.row("white_ring") is not None, timeout=WAIT_MS)
    row = win.light_panel.row("white_ring")
    assert row is not None
    row.level_slider.setValue(200)  # 20 %
    qtbot.waitUntil(lambda: gui_service.list_lights()[0]["level_pct"] == 20.0, timeout=WAIT_MS)
    win.lights_off_action.trigger()
    qtbot.waitUntil(lambda: all(c["level_pct"] == 0.0 for c in gui_service.list_lights()))
    qtbot.waitUntil(lambda: row.level() == 0.0, timeout=WAIT_MS)


def test_classic_tune_runs_in_the_worker(qtbot: QtBot, open_window: MainWindow) -> None:
    win = open_window
    win.tune_action.trigger()
    assert not win.tune_action.isEnabled()  # busy while tuning
    qtbot.waitUntil(lambda: win.warning_label.text().startswith("Tune"), timeout=60_000)
    assert win.tune_action.isEnabled()


def test_calibration_warning_goes_to_the_status_bar(qtbot: QtBot, open_window: MainWindow) -> None:
    # a readback outside the limits is not needed: use a ROI change with a calibration file absent
    # (no warning) and the window's own warning slot with a synthetic ApplyResult instead.
    from camera_lab.two_d.models import ApplyResult

    view = open_window.service.get_settings(SIM)
    res = ApplyResult(settings=view.settings, warnings=("ROI change may invalidate calibration",))
    open_window._show_apply_outcome(res, view)
    assert open_window.warning_label.text() == "ROI change may invalidate calibration"


def test_busy_camera_shows_the_lock_holder(qtbot: QtBot, window: MainWindow) -> None:
    other = CameraLock(SIM, "other").acquire()
    try:
        window.camera_list_panel._buttons[SIM].click()
        qtbot.waitUntil(lambda: window.lock_label.text().startswith("lock: busy"), timeout=WAIT_MS)
        assert "other" in window.lock_label.text()
        assert window.camera_id is None
    finally:
        other.release()


def test_dark_palette_applied(window: MainWindow) -> None:
    app = QApplication.instance()
    assert isinstance(app, QApplication)
    from PySide6.QtGui import QPalette

    pal = app.palette()
    assert pal.color(QPalette.ColorRole.Window).name() == styles.DARK_BG
    assert pal.color(QPalette.ColorRole.WindowText).name() == styles.TEXT_PRIMARY
    assert pal.color(QPalette.ColorRole.Base).name() == styles.DARK_BG_ALT
    assert pal.color(QPalette.ColorRole.Highlight).name() == styles.ACCENT_BLUE
    assert styles.DARK_BG in app.styleSheet()
    assert styles.ACCENT_RED in app.styleSheet()


def test_closing_releases_the_lock_and_turns_lights_off(
    qtbot: QtBot,
    open_window: MainWindow,
    gui_service: TwoDService,
    spy: Callable[[TwoDService, str], Spy],
) -> None:
    win = open_window
    off = spy(gui_service, "lights_off")
    qtbot.waitUntil(lambda: win.light_panel.row("red") is not None, timeout=WAIT_MS)
    row = win.light_panel.row("red")
    assert row is not None
    row.level_slider.setValue(100)
    qtbot.waitUntil(
        lambda: any(c["level_pct"] > 0 for c in gui_service.list_lights()), timeout=WAIT_MS
    )
    with_probe_busy = CameraLock(SIM, "probe")
    from camera_lab.core.locks import CameraBusyError

    try:
        with_probe_busy.acquire()
    except CameraBusyError as exc:
        assert "gui" in exc.holder  # the GUI owns the lock while open
    else:  # pragma: no cover
        raise AssertionError("camera should be locked by the gui")
    win.close()
    assert not win.worker_thread.isRunning()
    assert len(off.calls) >= 1
    assert all(c["level_pct"] == 0.0 for c in gui_service.list_lights())
    assert not gui_service.is_open(SIM)
    probe = CameraLock(SIM, "probe").acquire()  # the lock is free again
    probe.release()


def test_live_loop_emits_frames_within_the_rate_cap(qtbot: QtBot, open_window: MainWindow) -> None:
    win = open_window
    frames: list[tuple[Any, dict[str, Any]]] = []
    win.worker.frame_ready.connect(lambda img, stats: frames.append((img, stats)))
    win.live_action.setChecked(True)
    qtbot.waitUntil(lambda: len(frames) >= 8, timeout=30_000)
    img, stats = frames[-1]
    assert not img.isNull()
    assert img.width() <= 640
    assert 0.0 <= stats["mean"] <= 1.0
    assert 0.0 <= stats["clipped_pct"] <= 100.0
    assert len(stats["histogram"]) == 256
    assert abs(sum(stats["histogram"]) - 1.0) < 1e-6
    assert 0.0 < stats["fps"] <= 15.5
    assert win.live_view.has_image() and win.histogram.has_data()
    assert win.fps_label.text().endswith("fps")
    assert win.state_label.text().endswith("live")
    win.live_action.setChecked(False)
    qtbot.waitUntil(lambda: win.live_action.text() == "Start live", timeout=WAIT_MS)
    assert not win.worker.live


def test_clip_overlay_toggle_marks_clipped_pixels(qtbot: QtBot, open_window: MainWindow) -> None:
    win = open_window
    row = win.camera_settings_panel.row("exposure_us")
    assert row is not None
    row.value_spinbox.setValue(150_000.0)  # overexposed: lots of clipped pixels
    qtbot.waitUntil(
        lambda: win.service.get_settings(SIM).settings.exposure_us == 150_000.0, timeout=WAIT_MS
    )
    frames: list[dict[str, Any]] = []
    win.worker.frame_ready.connect(lambda img, stats: frames.append(stats))
    win.live_action.setChecked(True)
    qtbot.waitUntil(lambda: len(frames) >= 2, timeout=30_000)
    assert frames[-1]["clipped_pct"] > 0
    assert frames[-1]["clip_overlay"] is not None
    plain = win.live_view.image_label.pixmap().toImage()
    win.live_view.overlay_checkbox.setChecked(True)
    with_overlay = win.live_view.image_label.pixmap().toImage()
    assert plain != with_overlay
    win.live_action.setChecked(False)


def test_heartbeat_max_stall_stays_below_200_ms_during_live_view(
    qtbot: QtBot, open_window: MainWindow
) -> None:
    win = open_window
    win.heartbeat.reset()
    win.live_action.setChecked(True)
    start = time.monotonic()
    fps = []
    while time.monotonic() - start < 4.0:
        qtbot.wait(100)
        fps.append(win.measured_live_fps)
    win.live_action.setChecked(False)
    assert win.worker.frames_emitted >= 5
    assert win.heartbeat.ticks > 50
    assert win.max_ui_stall_ms < 200.0, win.max_ui_stall_ms
    get_logger("gui_test").info(
        "gui_bench_sim", live_fps=round(fps[-1], 1), max_ui_stall_ms=round(win.max_ui_stall_ms, 1)
    )


def test_heartbeat_detects_a_stall(qtbot: QtBot) -> None:
    from PySide6.QtCore import QTimer

    from camera_lab.two_d.gui.heartbeat import UiHeartbeat

    hb = UiHeartbeat()
    hb.start()
    QTimer.singleShot(30, lambda: time.sleep(0.12))
    qtbot.wait(400)
    hb.stop()
    assert 90.0 < hb.max_stall_ms < 400.0
    assert not hb.running


def test_unknown_job_is_reported(qtbot: QtBot, window: MainWindow) -> None:
    window.job_requested.emit("no_such_job", {})
    qtbot.waitUntil(lambda: "unknown job" in window.warning_label.text(), timeout=WAIT_MS)
