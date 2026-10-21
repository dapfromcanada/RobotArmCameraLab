"""Widget-level tests: factories (populate -> clear leaves nothing), settings rows, dialog,
pure helpers, CLI wiring."""

from __future__ import annotations

import dataclasses
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QLayout, QWidget
from pytestqt.qtbot import QtBot
from typer.testing import CliRunner

from camera_lab.core.records import CaptureSidecar, RunSummary
from camera_lab.two_d.gui.widgets.camera_list_panel import CameraListPanel
from camera_lab.two_d.gui.widgets.camera_settings_panel import CameraSettingsPanel
from camera_lab.two_d.gui.widgets.histogram_widget import HistogramWidget
from camera_lab.two_d.gui.widgets.light_panel import LightPanel
from camera_lab.two_d.gui.widgets.live_view_widget import LiveViewWidget
from camera_lab.two_d.gui.widgets.preset_dialog import PresetDialog
from camera_lab.two_d.gui.widgets.run_browser_panel import RunBrowserPanel
from camera_lab.two_d.gui.widgets.score_panel import ScorePanel
from camera_lab.two_d.gui.widgets.setting_row_widget import SettingRowWidget
from camera_lab.two_d.gui.workers import build_live_frame, fast_scores, processor_results
from camera_lab.two_d.models import (
    CameraInfo,
    MetricResult,
    ProcessorResult,
    ScoreReport,
    SettingSpec,
)
from camera_lab.two_d.service import TwoDService

from .conftest import SIM

RUN_ID = "20261021-101010_SIM-0001_run"


def flush_deletes() -> None:
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def assert_empty(container: QWidget, layout: QLayout) -> None:
    flush_deletes()
    assert layout.count() == 0
    assert container.findChildren(QWidget) == []


def sidecar(n: int) -> CaptureSidecar:
    return CaptureSidecar(
        capture_id=f"{RUN_ID}/{n:04d}",
        timestamp_utc=datetime.now(UTC),
        settings={"exposure_us": 5000.0, "gain_db": 1.5},
        lights={},
        pixel_format="Mono8",
        bit_depth=8,
        width=4,
        height=4,
        channels=1,
        dtype="uint8",
        scores={"composite": 50.0 + n},
    )


def run_summary(n: int) -> RunSummary:
    return RunSummary(
        run_id=f"20261021-10101{n}_SIM-0001_run",
        camera_serial=SIM,
        method="manual",
        operator="human",
        started_at=datetime.now(UTC),
        finished_at=None,
        capture_count=n,
        best_capture_id=None,
        best_score=None,
    )


@pytest.fixture
def opened(gui_service: TwoDService):  # type: ignore[no-untyped-def]
    gui_service.open(SIM, "test")
    return gui_service


# ------------------------------------------------- factories: populate -> clear
def test_camera_row_factory_populate_and_clear(qtbot: QtBot) -> None:
    panel = CameraListPanel()
    qtbot.addWidget(panel)
    cams = [
        CameraInfo(vendor="v", model=f"m{i}", serial=f"S{i}", driver="simulated") for i in range(3)
    ]
    panel.populate_cameras(cams)
    assert panel.camera_ids() == ["S0", "S1", "S2"]
    picked: list[str] = []
    panel.camera_selected.connect(picked.append)
    panel._buttons["S1"].click()
    assert picked == ["S1"]
    panel.set_open_camera("S2")
    assert panel._state_labels["S2"].text() == "open"
    panel.clear_cameras()
    assert_empty(panel.camera_container, panel.camera_layout)
    assert panel.camera_ids() == []
    assert panel.camera_button_group.buttons() == []


def test_setting_row_factory_populate_and_clear(qtbot: QtBot, opened: TwoDService) -> None:
    panel = CameraSettingsPanel()
    qtbot.addWidget(panel)
    view = opened.get_settings(SIM)
    panel.populate_settings(view)
    assert len(panel.row_names()) == len(view.ranges) + len(view.options)
    assert panel.rows_container.findChildren(SettingRowWidget)
    panel.clear_settings()
    assert_empty(panel.rows_container, panel.rows_layout)
    assert panel.row_names() == []


def test_light_row_factory_populate_and_clear(qtbot: QtBot, opened: TwoDService) -> None:
    panel = LightPanel()
    qtbot.addWidget(panel)
    channels = opened.list_lights()
    panel.populate_lights(channels)
    assert panel.channel_ids() == [c["id"] for c in channels]
    row = panel.row("white_ring")
    assert row is not None
    assert row.level_slider.maximum() == 800  # safety max 80 % at 0.1 % steps
    panel.clear_lights()
    assert_empty(panel.rows_container, panel.rows_layout)


def test_score_and_processor_row_factories_populate_and_clear(qtbot: QtBot) -> None:
    panel = ScorePanel()
    qtbot.addWidget(panel)
    report = ScoreReport(
        metrics=(
            MetricResult(name="mean_level", value=0.45),
            MetricResult(name="clipped_pct", value=0.2, unit="%", passed=True),
            MetricResult(name="black_pct", value=9.0, unit="%", passed=False),
        ),
        histogram=(0.5, 0.5),
        sub_scores={},
        composite=87.25,
        params={},
    )
    panel.populate_scores(report)
    assert panel.composite_label.text() == "87.2" or panel.composite_label.text() == "87.3"
    assert len(panel.metrics_container.findChildren(QWidget, "scoreRow")) == 3
    panel.populate_processors([ProcessorResult(name="demosaic", result={"shape": [4, 4, 3]})] * 2)
    assert len(panel.processors_container.findChildren(QWidget, "processorRow")) == 2
    assert panel.processors_title_label.isVisibleTo(panel)
    panel.clear_scores()
    panel.clear_processors()
    assert_empty(panel.metrics_container, panel.metrics_layout)
    assert_empty(panel.processors_container, panel.processors_layout)
    assert panel.composite_label.text() == "-"


def test_run_and_capture_factories_populate_and_clear(qtbot: QtBot, tmp_path: Path) -> None:
    panel = RunBrowserPanel(runs_root=tmp_path)
    qtbot.addWidget(panel)
    panel.populate_runs([run_summary(1), run_summary(2)])
    assert len(panel.run_ids()) == 2
    panel.populate_captures([sidecar(1), sidecar(2), sidecar(3)])
    assert len(panel.capture_ids()) == 3
    panel.select_capture(f"{RUN_ID}/0001")
    panel.select_capture(f"{RUN_ID}/0002")
    assert panel.compared_ids() == [f"{RUN_ID}/0001", f"{RUN_ID}/0002"]
    panel.clear_captures()
    assert_empty(panel.captures_container, panel.captures_layout)
    assert panel.compared_ids() == []
    panel.clear_runs()
    assert_empty(panel.runs_container, panel.runs_layout)
    assert panel.run_ids() == []
    assert panel.run_button_group.buttons() == []


def test_repopulating_replaces_rows(qtbot: QtBot) -> None:
    panel = ScorePanel()
    qtbot.addWidget(panel)
    for _ in range(3):
        panel.populate_processors([ProcessorResult(name="p", result={})] * 2)
    flush_deletes()
    assert len(panel.processors_container.findChildren(QWidget, "processorRow")) == 2


# ------------------------------------------------------------ settings panel
def test_settings_absent_in_hdr_mode_are_handled(qtbot: QtBot, opened: TwoDService) -> None:
    """The Triton in HDR mode has no plain exposure_us/gain_db: no rows, no errors."""
    panel = CameraSettingsPanel()
    qtbot.addWidget(panel)
    full = opened.get_settings(SIM)
    hdr_ranges = {k: v for k, v in full.ranges.items() if k not in ("exposure_us", "gain_db")}
    hdr_view = dataclasses.replace(full, ranges=hdr_ranges)
    panel.populate_settings(full)
    assert "exposure_us" in panel.row_names()
    panel.show_view(hdr_view)  # the set of settings changed -> rebuilt
    names = panel.row_names()
    assert "exposure_us" not in names and "gain_db" not in names
    assert "hdr.ch0.exposure_us" in names and "hdr.ch0.exposure_us" in names
    panel.show_view(full)
    assert "exposure_us" in panel.row_names()


def test_advanced_enum_is_a_combo_and_numeric_is_a_slider(
    qtbot: QtBot, opened: TwoDService
) -> None:
    panel = CameraSettingsPanel()
    qtbot.addWidget(panel)
    panel.show()
    panel.populate_settings(opened.get_settings(SIM))
    qtbot.wait(50)  # let the layout show the new rows
    enum_row = panel.row("hdr.enabled")
    num_row = panel.row("hdr.ch0.exposure_us")
    assert enum_row is not None and num_row is not None
    assert enum_row.option_combo.isVisibleTo(panel) and not enum_row.value_slider.isVisibleTo(panel)
    assert num_row.value_slider.isVisibleTo(panel) and not num_row.option_combo.isVisibleTo(panel)
    assert num_row.unit_label.text() == "us"
    patches = []
    panel.patch_requested.connect(patches.append)
    enum_row.option_combo.setCurrentText("on")
    num_row.value_spinbox.setValue(1234)
    qtbot.waitUntil(lambda: len(patches) == 1)
    assert patches[0].advanced == {"hdr.enabled": "on", "hdr.ch0.exposure_us": 1234.0}


def test_row_value_set_from_readback_does_not_emit(qtbot: QtBot) -> None:
    spec = SettingSpec(name="gain_db", kind="float", min=0, max=18, increment=0.1, unit="dB")
    row = SettingRowWidget(spec, 3.0)
    qtbot.addWidget(row)
    seen: list[tuple[str, object]] = []
    row.value_changed.connect(lambda n, v: seen.append((n, v)))
    row.set_value(7.5)
    assert seen == []
    assert row.value_spinbox.value() == 7.5
    row.value_slider.setValue(500)
    assert seen == [("gain_db", 9.0)]
    row.show_result("clamped", "rounded")
    assert row.reason_text().startswith("adjusted")
    row.show_result("applied", None)
    assert row.reason_text() == ""


def test_exposure_slider_uses_log_scale_and_snaps_to_increment(qtbot: QtBot) -> None:
    spec = SettingSpec(name="exposure_us", kind="float", min=20, max=200000, increment=1, unit="us")
    row = SettingRowWidget(spec, 5000.0)
    qtbot.addWidget(row)
    mid = row.value_from_slider(500)
    assert 1000 < mid < 3000  # geometric middle, not 100010
    assert mid == round(mid)
    assert row.value_from_slider(0) == 20
    assert row.value_from_slider(1000) == 200000


# ------------------------------------------------------------ views + dialog
def test_histogram_paints_and_clears(qtbot: QtBot) -> None:
    hist = HistogramWidget()
    qtbot.addWidget(hist)
    hist.resize(300, 120)
    hist.set_histogram([0.1] * 256, 0.5, 2.0)
    assert hist.has_data()
    image = hist.grab().toImage()
    assert not image.isNull() and image.width() == 300
    hist.clear_histogram()
    assert not hist.has_data()
    hist.grab()


def test_live_view_overlay_changes_the_picture(qtbot: QtBot) -> None:
    view = LiveViewWidget()
    qtbot.addWidget(view)
    view.resize(400, 300)
    view.show()
    base = QImage(64, 48, QImage.Format.Format_RGB888)
    base.fill(0x202020)
    overlay = QImage(64, 48, QImage.Format.Format_RGBA8888)
    overlay.fill(0xFF0000FF)
    toggles: list[bool] = []
    view.overlay_toggled.connect(toggles.append)
    view.show_frame(base, overlay)
    plain = view.image_label.pixmap().toImage()
    view.overlay_checkbox.setChecked(True)
    assert toggles == [True]
    assert view.image_label.pixmap().toImage() != plain
    view.clear_frame()
    assert not view.has_image()


def test_preset_dialog_save_validation(qtbot: QtBot) -> None:
    dialog = PresetDialog("save", [("run/0001", "run/0001 (score 80)")])
    qtbot.addWidget(dialog)
    from PySide6.QtWidgets import QDialogButtonBox

    ok = dialog.button_box.button(QDialogButtonBox.StandardButton.Ok)
    assert not ok.isEnabled()
    dialog.name_input.setText("good-name_1")
    assert ok.isEnabled()
    assert dialog.preset_name() == "good-name_1"
    assert dialog.choice_value() == "run/0001"
    dialog.name_input.setText("")
    assert not ok.isEnabled()


def test_preset_dialog_apply_mode(qtbot: QtBot) -> None:
    dialog = PresetDialog("apply", [("a", "a"), ("b", "b")])
    qtbot.addWidget(dialog)
    from PySide6.QtWidgets import QDialogButtonBox

    assert dialog.windowTitle() == "Apply preset"
    assert not dialog.name_input.isVisibleTo(dialog)
    assert dialog.button_box.button(QDialogButtonBox.StandardButton.Ok).isEnabled()
    dialog.choice_combo.setCurrentIndex(1)
    assert dialog.preset_name() == "b"


# ------------------------------------------------------------ pure helpers
def test_fast_scores_on_a_clipped_image() -> None:
    import numpy as np

    img = np.zeros((10, 10, 3), dtype=np.uint8)
    img[:5] = 255
    stats = fast_scores(img)
    assert stats["mean"] == pytest.approx(0.5, abs=1e-3)
    assert stats["clipped_pct"] == pytest.approx(50.0)
    assert sum(stats["histogram"]) == pytest.approx(1.0)
    assert stats["histogram"][0] == pytest.approx(0.5) and stats["histogram"][255] == pytest.approx(
        0.5
    )


def test_build_live_frame_and_processor_results(opened: TwoDService) -> None:
    frame = opened.grab(SIM)
    image, stats = build_live_frame(frame)
    assert max(image.width(), image.height()) <= 640
    assert stats["frame_id"] == frame.frame_id and stats["width"] == frame.width
    assert stats["pixel_format"] == frame.pixel_format
    results = processor_results(frame)
    assert [r.name for r in results] == ["demosaic", "make_preview", "to_luma"]
    assert results[0].result["shape"] == [frame.height, frame.width, 3]


# ----------------------------------------------------------------------- app + CLI
RUN_GUI_SCRIPT = """
import sys
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from camera_lab.two_d.service import TwoDService
from camera_lab.two_d.gui.app import run_gui

TwoDService._load_real_drivers = lambda self: None  # sim only, never the real cameras
app = QApplication([])
QTimer.singleShot(1500, app.quit)
sys.exit(run_gui(sim=True))
"""


def test_run_gui_runs_and_shuts_down_in_its_own_process() -> None:
    """``app.exec()`` poisons the shared qapp of the test session, so run it in a subprocess."""
    done = subprocess.run(
        [sys.executable, "-c", RUN_GUI_SCRIPT],
        capture_output=True,
        check=False,
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
        timeout=60,
    )
    assert done.returncode == 0, done.stderr.decode()
    assert b"ui_heartbeat" in done.stderr
    assert b"gui_exit" in done.stderr


def test_cli_gui_command_calls_run_gui(monkeypatch: pytest.MonkeyPatch) -> None:
    from camera_lab.cli import app

    seen: dict[str, object] = {}

    def fake_run_gui(sim: bool = False, service: object = None) -> int:
        seen["sim"] = sim
        return 0

    monkeypatch.setattr("camera_lab.two_d.gui.app.run_gui", fake_run_gui)
    result = CliRunner().invoke(app, ["gui", "--sim"])
    assert result.exit_code == 0, result.output
    assert seen == {"sim": True}
    assert CliRunner().invoke(app, ["--sim", "gui"]).exit_code == 0
    assert "gui" in CliRunner().invoke(app, ["--help"]).output


def test_importing_the_cli_does_not_import_qt() -> None:
    code = "import sys, camera_lab.cli; sys.exit(1 if 'PySide6' in sys.modules else 0)"
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, check=False)
    assert done.returncode == 0, done.stderr.decode()


def test_gui_code_has_no_direct_service_calls_from_the_ui_thread() -> None:
    """Only the worker talks to the service; the window just hands it over."""
    root = Path(__file__).resolve().parents[3] / "src" / "camera_lab" / "two_d" / "gui"
    window = (root / "main_window.py").read_text()
    uses = [ln.strip() for ln in window.splitlines() if "self.service" in ln]
    assert uses == [
        "self.service = service  # data only: handed to the worker, never called from here",
        "self.worker = CameraWorker(self.service)",
    ]
    for path in sorted((root / "widgets").glob("*.py")):
        assert ".service." not in path.read_text(), path.name
        assert "import TwoDService" not in path.read_text(), path.name
