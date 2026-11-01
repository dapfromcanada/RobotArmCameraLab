"""Phase 10 step 5: the gated camera setup procedure (simulated camera + rendered charts)."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from camera_lab.two_d.charts import init_builtin
from camera_lab.two_d.errors import InvalidRequestError
from camera_lab.two_d.models import CameraSettingsPatch
from camera_lab.two_d.service import TwoDService
from camera_lab.two_d.setup import SetupSession

from .scene import Scene

SIM = "SIM-0001"


@pytest.fixture
def svc(make_service: Callable[..., TwoDService], tmp_path: Path) -> TwoDService:
    init_builtin(tmp_path / "charts")
    s = make_service(charts_root=tmp_path / "charts", backups_root=tmp_path / "backups")
    s.open(SIM, "test")
    return s


def _session(
    svc: TwoDService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **scene: Any
) -> tuple[SetupSession, Scene]:
    sc = Scene(svc, **scene)
    monkeypatch.setattr(svc, "grab", sc.grab)
    return SetupSession(svc, SIM, sleep=lambda s: None, root=tmp_path / "setup"), sc


def test_full_setup_passes_and_saves(
    svc: TwoDService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ses, _ = _session(svc, tmp_path, monkeypatch)
    first = ses.step_backup()
    assert first.ok and "camera in mode linear" in first.message
    assert svc.current_mode(SIM) == "linear"  # the setup enters the mode it tunes
    assert ses.step_light().ok
    b = ses.step_brightness()
    assert b.status == "pass", b.message
    assert "step wedge paper white" in b.message
    reading = ses.focus_reading()
    assert reading["mtf50_cyc_px"] and reading["best"] == reading["mtf50_cyc_px"]
    f = ses.finish_focus(locked=True)
    assert f.status == "pass" and "baseline" in f.message
    assert (tmp_path / "setup" / f"{SIM}.json").is_file()
    assert ses.step_brightness_recheck().ok
    wb = ses.step_white_balance()
    assert wb.status == "pass", wb.message  # the 1.2 / 0.9 cast is corrected
    assert ses.next_step() == "save"
    saved = ses.save(confirm=True, preset_name="bench-setup")
    assert saved.status == "pass", saved.message
    assert saved.data["user_set"]["slot"] == "UserSet1"
    assert saved.data["user_set"]["backup_before"]
    assert svc.get_preset(SIM, "bench-setup") is not None
    report = json.loads((ses.run.recorder.run_dir / "setup_report.json").read_text())  # type: ignore[union-attr]
    assert [s["step"] for s in report["steps"]] == [
        "backup",
        "light",
        "brightness",
        "focus",
        "brightness_recheck",
        "white_balance",
        "save",
    ]
    assert ses.next_step() is None


def test_second_setup_compares_focus_with_the_baseline(
    svc: TwoDService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ses, _ = _session(svc, tmp_path, monkeypatch)
    base = tmp_path / "setup" / f"{SIM}.json"
    base.parent.mkdir(parents=True)
    base.write_text(json.dumps({"mtf50_cyc_px": 0.45}))  # much sharper than this lens / blur
    ses.step_backup(), ses.step_light(), ses.step_brightness()
    f = ses.finish_focus(locked=True)
    assert f.status == "fail" and "drifted or the lens was bumped" in f.message
    assert ses.next_step() == "focus"


def test_focus_uses_the_focus_meter_and_compares_on_the_same_chart(
    svc: TwoDService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The setup reads focus with the Focus tool's meter (its hint comes along), records the
    chart in the baseline and compares only on that chart."""
    ses, _ = _session(svc, tmp_path, monkeypatch)
    ses.step_backup(), ses.step_light(), ses.step_brightness()
    reading = ses.focus_reading()
    assert reading["chart"] and "turn" in reading["message"]
    assert ses.finish_focus(locked=True).status == "pass"
    base = tmp_path / "setup" / f"{SIM}.json"
    assert json.loads(base.read_text())["chart"] == reading["chart"]
    base.write_text(json.dumps({"mtf50_cyc_px": 0.01, "chart": "another-chart-v1"}))
    f = ses.finish_focus(locked=True)
    assert f.status == "fail" and "measured on chart another-chart-v1" in f.message


def test_focus_step_lowers_an_exposure_that_clips_the_chart(
    svc: TwoDService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The brightness step sets the exposure on the grey patch; if that clips the chart white,
    the focus step gives no number and lowers the exposure like the Focus tool (2026-11-01)."""
    ses, sc = _session(svc, tmp_path, monkeypatch)
    ses.step_backup(), ses.step_light(), ses.step_brightness()
    good = ses.focus_reading()["mtf50_cyc_px"]
    sc.k *= 4  # more light on the chart than on the grey patch: the white well past full scale
    exposure = svc.get_settings(SIM).settings.exposure_us
    hot = ses.focus_reading()
    assert hot["mtf50_cyc_px"] is None and "chart white clipped" in hot["message"]
    assert svc.get_settings(SIM).settings.exposure_us < exposure  # lowered
    for _ in range(5):
        reading = ses.focus_reading()
        if reading["mtf50_cyc_px"] is not None:
            break
    assert reading["mtf50_cyc_px"] == pytest.approx(good, rel=0.1)
    assert ses.finish_focus(locked=True).status == "pass"
    assert ses.step_brightness_recheck().ok  # the grey patch level is set again


@pytest.mark.parametrize(
    ("k", "start_exposure", "expect"),
    [
        # visible but too dark even at the 200 ms working max and 18 dB: 4e-7 × 2e5 × 7.9 < 0.85
        (4e-7, 200_000.0, "open the iris or add light"),
        # clipped even at the camera's 10 µs minimum: 0.2 × 10 > 1
        (0.2, 10.0, "close the iris"),
    ],
)
def test_brightness_stops_with_what_it_needs(
    svc: TwoDService,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    k: float,
    start_exposure: float,
    expect: str,
) -> None:
    svc.apply(SIM, CameraSettingsPatch(exposure_us=start_exposure, gain_db=0))
    ses, _ = _session(svc, tmp_path, monkeypatch, k=k)
    ses.step_backup()
    assert ses.step_light().ok  # the chart is visible
    b = ses.step_brightness()
    assert b.status == "fail" and expect in b.message, b.message
    assert b.data["history"]


def test_unstable_light_stops(
    svc: TwoDService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ses, sc = _session(svc, tmp_path, monkeypatch)
    sc.drift = sc.k * 0.1  # +10 % per frame: sunrise
    ses.step_backup()
    r = ses.step_light()
    assert r.status == "fail" and "lighting is not stable" in r.message


def test_no_chart_in_view(
    svc: TwoDService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ses, sc = _session(svc, tmp_path, monkeypatch)
    sc.page = np.full_like(sc.page, 0.5)
    ses.step_backup()
    r = ses.step_light()
    assert r.status == "fail" and "place one under the camera" in r.message


def test_order_and_confirmation_are_enforced(
    svc: TwoDService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ses, _ = _session(svc, tmp_path, monkeypatch)
    with pytest.raises(InvalidRequestError, match="backup"):
        ses.step_light()
    with pytest.raises(InvalidRequestError, match="confirm=True"):
        svc.save_user_set(SIM, confirm=False)
    ses.step_backup(), ses.step_light(), ses.step_brightness()
    assert ses.finish_focus(locked=False).status == "fail"
    assert ses.finish_focus(locked=True).ok
    ses.step_brightness_recheck(), ses.step_white_balance()
    r = ses.save(confirm=False)
    assert r.status == "fail" and "did not confirm" in r.message
    assert not hasattr(svc._open[SIM].camera, "user_sets")  # type: ignore[attr-defined]


def test_cli_setup_stops_and_says_why(tmp_roots: Path) -> None:
    from typer.testing import CliRunner

    from camera_lab.cli import app

    res = CliRunner().invoke(app, ["--sim", "two-d", "setup", "sim", "--focus-locked"])
    assert res.exit_code == 4, res.output
    assert "[ok  ] backup" in res.stdout
    assert "[STOP] light: no step wedge or grey patch in view" in res.stdout
    assert "setup_report.json" in res.stdout


def test_arena_save_user_set(tmp_roots: Path) -> None:
    from camera_lab.core.lights import LightService, SimulatedLight
    from camera_lab.core.safety import load_safety_limits
    from camera_lab.two_d.drivers.arena import ArenaDriver

    from .fake_arena import FakeSystem

    limits = load_safety_limits()
    system = FakeSystem()
    svc = TwoDService(
        limits, LightService(SimulatedLight(), limits), drivers={"arena": ArenaDriver(system)}
    )
    try:
        svc.open("233100505", "test")
        svc.grab("233100505")  # streaming: the save pauses it
        out = svc.save_user_set("233100505", confirm=True)
        dev = system.created[-1]
        assert out["slot"] == "UserSet1" and out["default"] == "UserSet1"
        assert (
            "UserSet1" in dev.saved_user_sets and dev.nodemap["UserSetDefault"].value == "UserSet1"
        )
        assert "HDRTuningEnable" not in out["stored_features"] and out["backup_before"]
        assert dev.streaming
        with pytest.raises(InvalidRequestError, match="no user set"):
            svc.save_user_set("233100505", "UserSet9", confirm=True)
    finally:
        svc.shutdown()


def test_ebus_save_user_set_has_no_boot_set(tmp_roots: Path) -> None:
    from camera_lab.core.lights import LightService, SimulatedLight
    from camera_lab.core.safety import load_safety_limits
    from camera_lab.two_d.drivers.ebus import EbusDriver

    from .fake_ebus import FakeSdk

    limits = load_safety_limits()
    sdk = FakeSdk()
    svc = TwoDService(
        limits, LightService(SimulatedLight(), limits), drivers={"ebus": EbusDriver(sdk)}
    )
    try:
        svc.open("U330439", "test")
        svc.grab("U330439")  # streaming: the JAI refuses UserSetSave unless stopped (live)
        out = svc.save_user_set("U330439", "UserSet2", confirm=True)
        assert sdk.connected[-1].cam.streaming  # restarted after the save
        assert out["default"] is None and "no UserSetDefault" in out["notes"][0]
        assert "UserSet2" in sdk.connected[-1].cam.saved_user_sets
    finally:
        svc.shutdown()


def test_too_bright_with_gain_takes_gain_off_first(
    svc: TwoDService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc.apply(SIM, CameraSettingsPatch(exposure_us=20_000, gain_db=18))
    ses, _ = _session(svc, tmp_path, monkeypatch)  # white ≈ 0.8 × 7.9: far too bright
    ses.step_backup(), ses.step_light()
    b = ses.step_brightness()
    assert b.status == "pass", b.message
    assert svc.get_settings(SIM).settings.gain_db < 6  # gain came down rather than exposure only


def test_save_failure_is_recorded_with_the_camera_answer(
    svc: TwoDService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from camera_lab.two_d.errors import DeviceError

    ses, _ = _session(svc, tmp_path, monkeypatch)
    ses.step_backup(), ses.step_light(), ses.step_brightness()
    ses.finish_focus(locked=True), ses.step_brightness_recheck(), ses.step_white_balance()

    def refuse(*a: Any, **k: Any) -> Any:
        raise DeviceError("UserSetSave: GENERIC_ERROR: Node is not writable.")

    monkeypatch.setattr(svc, "save_user_set", refuse)
    r = ses.save(confirm=True)
    assert r.status == "fail" and "Node is not writable" in r.message
    report = json.loads((ses.run.recorder.run_dir / "setup_report.json").read_text())  # type: ignore[union-attr]
    assert report["steps"][-1]["status"] == "fail"
    steps = ses.run.recorder.read_steps()  # type: ignore[union-attr]
    assert steps[-1].tool == "setup.save" and "Node is not writable" in str(steps[-1].result)


def test_setup_save_changes_the_boot_set_only_on_site(
    make_service: Callable[..., TwoDService],
) -> None:
    svc = make_service()
    svc.open("SIM-0001", "test")
    cam = svc._open["SIM-0001"].camera
    cam.save_user_set("UserSet1", make_default=False)  # lab: saved, boot set unchanged
    assert svc.user_set_info("SIM-0001")["boot_set"] == "Default"
    cam.save_user_set("UserSet1", make_default=True)  # on site
    assert svc.user_set_info("SIM-0001")["boot_set"] == "UserSet1"
