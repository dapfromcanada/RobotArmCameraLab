"""Tests for TwoDService: open/lock, apply safety flow, grab, capture, runs, presets, lights."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import tifffile

from camera_lab.core.lights import SimulatedLight
from camera_lab.core.records import load_run
from camera_lab.two_d.drivers.simulated import SimulatedCamera, SimulatedCameraDriver
from camera_lab.two_d.errors import (
    AlreadyExistsError,
    CameraBusyError,
    CameraNotFoundError,
    CameraNotOpenError,
    CaptureTimeoutError,
    ConfigurationError,
    IncompleteFrameError,
    InvalidRequestError,
    LimitRejectedError,
    NotFoundError,
    StepBudgetExhausted,
)
from camera_lab.two_d.models import ROI, CameraInfo, CameraSettingsPatch
from camera_lab.two_d.service import TwoDService, build_service

from .conftest import tree

SIM = "SIM-0001"


def _cam(svc: TwoDService) -> SimulatedCamera:
    cam = svc._open[SIM].camera
    assert isinstance(cam, SimulatedCamera)
    return cam


def _by(res: Any) -> dict[str, Any]:
    return {r.field: r for r in res.results}


# ---------------------------------------------------------- discover/open
def test_discover_sim(make_service: Callable[..., TwoDService]) -> None:
    svc = make_service()
    [info] = svc.discover()
    assert info.serial == SIM


def test_discover_skips_foreign_vendor_and_broken_driver(
    make_service: Callable[..., TwoDService],
) -> None:
    class Foreign(SimulatedCameraDriver):
        def discover(self) -> list[CameraInfo]:
            return [
                CameraInfo(vendor="JAI Corporation", model="X", serial="U1", driver="simulated")
            ]

    class Broken(SimulatedCameraDriver):
        def discover(self) -> list[CameraInfo]:
            raise RuntimeError("bus error")

    svc = make_service(drivers={"simulated": Foreign(), "arena": Broken()})
    assert svc.discover() == []


def test_open_close_and_lock(make_service: Callable[..., TwoDService]) -> None:
    a = make_service()
    b = make_service()
    a.open("SIM-0001", "gui")
    assert a.is_open(SIM)
    assert a.open(SIM, "gui").serial == SIM  # idempotent
    with pytest.raises(CameraBusyError, match="locked by pid") as ei:
        b.open(SIM, "mcp")
    assert ei.value.exit_code == 3 and "gui" in ei.value.holder
    a.close(SIM)
    a.close(SIM)
    b.open(SIM, "mcp")


def test_open_unknown_and_alias(make_service: Callable[..., TwoDService]) -> None:
    svc = make_service()
    with pytest.raises(CameraNotFoundError):
        svc.open("U330439")
    with pytest.raises(InvalidRequestError):
        svc.open("../etc")


def test_not_open_errors(make_service: Callable[..., TwoDService]) -> None:
    svc = make_service()
    for call in (
        lambda: svc.grab(SIM),
        lambda: svc.get_settings(SIM),
        lambda: svc.apply(SIM, CameraSettingsPatch(gain_db=1)),
        lambda: svc.capture(SIM),
        lambda: svc.start_run(SIM, "g", 5),
    ):
        with pytest.raises(CameraNotOpenError) as ei:
            call()
        assert ei.value.code == "camera_not_open"


# ----------------------------------------------------------- settings
def test_get_settings_effective_ranges(svc: TwoDService) -> None:
    view = svc.get_settings(SIM)
    # limits policy (schema 2): camera settings are limited only by the camera's range
    exp = view.ranges["exposure_us"]
    assert exp.min.value == 10 and exp.min.source == "camera min"  # type: ignore[union-attr]
    assert exp.max.value == 1_000_000  # type: ignore[union-attr]
    gain = view.ranges["gain_db"]
    assert gain.max.value == 24  # type: ignore[union-attr]
    fr = view.ranges["frame_rate_hz"]
    assert fr.max.value == 60  # type: ignore[union-attr]
    # the profile's working range is reported as guidance next to the camera range
    assert view.working["exposure_us"] == {"min": None, "max": 200_000}
    assert view.working["gain_db"] == {"min": 0, "max": 18}
    assert view.working["hdr.ch0.exposure_us"]["max"] == 200_000  # by unit
    assert view.options["pixel_format"] == ("Mono8", "Mono12", "BayerRG8", "RGB8")
    assert "roi_width" in view.affects_calibration
    d = view.to_dict()
    assert d["ranges"]["gain_db"]["max_source"] == "camera max"
    assert d["working_range"]["gain_db"]["max"] == 18
    json.dumps(d)


def test_apply_rejects_with_limit_reason(svc: TwoDService) -> None:
    res = svc.apply(SIM, CameraSettingsPatch(gain_db=30.0, exposure_us=1234.4, frame_rate_hz=80))
    by = _by(res)
    assert by["gain_db"].status == "rejected"
    assert by["gain_db"].reason == "gain_db 30.0 > camera max 24.0"
    assert by["exposure_us"].status == "clamped" and by["exposure_us"].value == 1234.0
    assert by["frame_rate_hz"].status == "rejected"
    assert res.settings.gain_db == 0.0 and res.settings.exposure_us == 1234.0
    assert not res.ok


def test_apply_clamp_mode_for_tuner(svc: TwoDService) -> None:
    res = svc.apply(SIM, CameraSettingsPatch(exposure_us=2e6, gain_db=-3), mode="clamp")
    by = _by(res)
    assert by["exposure_us"].status == "clamped" and res.settings.exposure_us == 1_000_000
    assert by["gain_db"].status == "clamped" and res.settings.gain_db == 0
    assert res.ok


def test_apply_camera_range_wb_roi_enums(svc: TwoDService) -> None:
    res = svc.apply(
        SIM,
        CameraSettingsPatch(
            gamma=9.0,
            white_balance={"red": 1.5, "blue": 0.1},
            roi=ROI(x=3, y=2, width=500, height=300),
            pixel_format="Mono16",
            trigger_mode="software",
        ),
    )
    by = _by(res)
    assert by["gamma"].status == "rejected" and "camera max" in (by["gamma"].reason or "")
    assert by["white_balance.red"].status == "applied"
    assert by["white_balance.blue"].status == "rejected"
    assert by["roi"].status == "clamped"
    assert res.settings.roi == ROI(x=4, y=2, width=496, height=300)
    assert by["pixel_format"].status == "rejected"
    assert by["trigger_mode"].status == "applied"
    bad_roi = svc.apply(SIM, CameraSettingsPatch(roi=ROI(x=0, y=0, width=8, height=8)))
    assert _by(bad_roi)["roi"].status == "rejected"


def test_apply_stops_stream_for_format_change(svc: TwoDService) -> None:
    svc.grab(SIM)
    assert _cam(svc).is_streaming
    res = svc.apply(SIM, CameraSettingsPatch(pixel_format="RGB8"))
    assert res.ok and not _cam(svc).is_streaming
    f = svc.grab(SIM)  # restarts the stream
    assert f.pixel_format == "RGB8" and _cam(svc).is_streaming
    svc.apply(SIM, CameraSettingsPatch(exposure_us=7000))
    assert _cam(svc).is_streaming  # exposure does not need a stop


def test_apply_respects_write_rate_limit(svc: TwoDService, clock: Any) -> None:
    start = clock.t
    for i in range(10):
        svc.apply(SIM, CameraSettingsPatch(exposure_us=1000 + i))
    assert clock.t - start == pytest.approx(1.0)  # 5 burst, then 5 at 5/s


def test_apply_calibration_warning(svc: TwoDService, tmp_roots: Path) -> None:
    (tmp_roots / "calibration" / "intrinsics").mkdir(parents=True)
    (tmp_roots / "calibration" / "intrinsics" / "SIM-0001.yaml").write_text("k")
    res = svc.apply(SIM, CameraSettingsPatch(roi=ROI(x=0, y=0, width=512, height=512)))
    assert any("may invalidate calibration" in w for w in res.warnings)
    assert svc.apply(SIM, CameraSettingsPatch(gain_db=1)).warnings == ()


def test_readback_outside_range_warns(svc: TwoDService, monkeypatch: pytest.MonkeyPatch) -> None:
    cam = _cam(svc)
    real_apply = cam.apply

    def lying_apply(patch: CameraSettingsPatch) -> Any:
        res = real_apply(patch)
        bad = res.settings.model_copy(update={"gain_db": 26.0})
        return res.model_copy(update={"settings": bad})

    monkeypatch.setattr(cam, "apply", lying_apply)
    res = svc.apply(SIM, CameraSettingsPatch(gain_db=3))
    assert any("read-back gain_db 26.0" in w for w in res.warnings)


# --------------------------------------------------------------- grab
def test_grab_is_non_recording(svc: TwoDService, tmp_roots: Path) -> None:
    before = tree(tmp_roots)
    frames = [svc.grab(SIM) for _ in range(3)]
    assert [f.frame_id for f in frames] == [1, 2, 3]
    assert all(f.pixel_format == "BayerRG8" and f.data.shape == (768, 1024) for f in frames)
    assert frames[0].settings == svc.get_settings(SIM).settings
    assert tree(tmp_roots) == before  # nothing written anywhere under WS
    assert svc.last_frame(SIM) is frames[-1]


def test_grab_returns_copies(svc: TwoDService) -> None:
    a = svc.grab(SIM)
    b = svc.grab(SIM)
    assert not np.shares_memory(a.data, b.data)


def test_grab_follows_settings(svc: TwoDService) -> None:
    svc.apply(
        SIM, CameraSettingsPatch(pixel_format="Mono12", roi=ROI(x=0, y=0, width=256, height=128))
    )
    f = svc.grab(SIM)
    assert (f.pixel_format, f.bit_depth, f.data.shape, f.data.dtype) == (
        "Mono12",
        12,
        (128, 256),
        np.uint16,
    )


def test_grab_timeout_and_incomplete(svc: TwoDService) -> None:
    svc.grab(SIM)
    _cam(svc).inject_fault("timeout")
    with pytest.raises(CaptureTimeoutError) as ei:
        svc.grab(SIM, timeout_ms=100)
    assert ei.value.exit_code == 6 and ei.value.code == "capture_timeout"
    _cam(svc).inject_fault("incomplete")
    with pytest.raises(IncompleteFrameError):
        svc.grab(SIM)


@pytest.mark.parametrize("bad", [0, -1, 60_001, 1.5, True])
def test_grab_bad_timeout(svc: TwoDService, bad: Any) -> None:
    with pytest.raises(InvalidRequestError):
        svc.grab(SIM, timeout_ms=bad)


def test_grab_uses_no_run_step(svc: TwoDService) -> None:
    run = svc.start_run(SIM, "g", 2)
    for _ in range(5):
        svc.grab(SIM)
    assert run.steps_used == 0
    assert len(run.recorder.read_steps()) == 1  # only start_tuning_run


# ------------------------------------------------------------- capture
def test_capture_manual_run(svc: TwoDService, tmp_roots: Path) -> None:
    [r1, r2] = svc.capture(SIM, 2, note="bench test")
    assert r1.run_id == r2.run_id and r1.run_id.endswith("_bench-test")
    rec = load_run(r1.run_id)
    assert rec.method == "manual" and rec.finished_at is not None
    assert rec.best_capture_id in (r1.capture_id, r2.capture_id)
    assert r1.preview_path is not None and r1.preview_path.exists()
    side = json.loads((r1.preview_path.parents[1] / "captures" / "0001.json").read_text())
    assert side["scores"]["composite"] == r1.scores.composite
    assert side["lights"] == {"white_ring": 0.0, "white_bar": 0.0, "red": 0.0, "blue": 0.0}
    assert r1.to_dict()["capture_id"] == r1.capture_id


def test_capture_into_existing_run(svc: TwoDService) -> None:
    run = svc.start_run(SIM, "goal", 10)
    [a] = svc.capture(SIM, 1, run.run_id, actor="agent")
    [b] = svc.capture(SIM)  # active run is used when run_id is omitted
    assert a.run_id == b.run_id == run.run_id
    assert run.steps_used == 2


def test_capture_run_errors(svc: TwoDService, make_service: Callable[..., TwoDService]) -> None:
    [done] = svc.capture(SIM)
    with pytest.raises(InvalidRequestError, match="already finished"):
        svc.capture(SIM, 1, done.run_id)
    with pytest.raises(NotFoundError):
        svc.capture(SIM, 1, "20261021-101500_SIM-0001_nope")
    with pytest.raises(InvalidRequestError):
        svc.capture(SIM, 0)
    with pytest.raises(InvalidRequestError):
        svc.capture(SIM, 1, note="x" * 501)


def test_capture_limit(svc: TwoDService, monkeypatch: pytest.MonkeyPatch) -> None:
    run = svc.start_run(SIM, "g", 40)
    monkeypatch.setattr(run.recorder, "max_captures", 1)
    svc.capture(SIM, 1, run.run_id)
    with pytest.raises(LimitRejectedError):
        svc.capture(SIM, 1, run.run_id)


def test_capture_timeout_propagates(svc: TwoDService) -> None:
    svc.grab(SIM)
    _cam(svc).inject_fault("timeout")
    with pytest.raises(CaptureTimeoutError):
        svc.capture(SIM)


# ---------------------------------------------------------------- runs
def test_run_budget_exhaustion(svc: TwoDService, sim_light: SimulatedLight) -> None:
    run = svc.start_run(SIM, "goal", 3)
    svc.apply(SIM, CameraSettingsPatch(exposure_us=9000))
    svc.capture(SIM)
    svc.set_lights({"white_ring": 10})
    assert run.steps_remaining == 0
    before = svc.get_settings(SIM).settings
    with pytest.raises(StepBudgetExhausted) as ei:
        svc.apply(SIM, CameraSettingsPatch(exposure_us=1000))
    assert ei.value.exit_code == 4
    with pytest.raises(StepBudgetExhausted):
        svc.capture(SIM)
    with pytest.raises(StepBudgetExhausted):
        svc.set_lights({"white_ring": 20})
    assert svc.get_settings(SIM).settings == before
    assert sim_light.get_levels()["white_ring"] == 10
    svc.lights_off("agent")  # always allowed
    tools = [s.tool for s in run.recorder.read_steps()]
    assert tools == [
        "start_tuning_run",
        "set_camera_settings",
        "capture",
        "set_lights",
        "lights_off",
    ]


def test_run_rejections_logged(svc: TwoDService) -> None:
    run = svc.start_run(SIM, "goal", 5)
    svc.apply(SIM, CameraSettingsPatch(gain_db=30), "agent")
    step = run.recorder.read_steps()[-1]
    assert step.actor == "agent" and step.rejections and "camera max 24.0" in step.rejections[0]


def test_start_run_limits(svc: TwoDService) -> None:
    with pytest.raises(LimitRejectedError, match=r"tuning\.max_steps_hard"):
        svc.start_run(SIM, "goal", 41)
    with pytest.raises(InvalidRequestError):
        svc.start_run(SIM, "x" * 501, 5)
    svc.start_run(SIM, "goal", 5)
    with pytest.raises(AlreadyExistsError):
        svc.start_run(SIM, "goal", 5)
    assert svc.active_run(SIM) is not None


def test_finish_run_with_preset(svc: TwoDService, tmp_roots: Path) -> None:
    run = svc.start_run(SIM, "find a good exposure", 10)
    [c] = svc.capture(SIM, 1, run.run_id, actor="agent")
    res = svc.finish_run(run.run_id, c.capture_id, "bench_ok", "done", "agent")
    assert res.preset_path is not None and res.preset_path.exists()
    assert res.record.preset_saved == str(res.preset_path)
    assert res.record.best_capture_id == c.capture_id and res.record.steps_used == 1
    assert svc.active_run(SIM) is None
    preset = svc.get_preset(SIM, "bench_ok")
    assert preset.created_by == "agent" and preset.source_capture_id == c.capture_id
    assert "histogram" not in preset.scores_at_save
    with pytest.raises(NotFoundError):
        svc.finish_run(run.run_id, None)


def test_finish_run_errors(svc: TwoDService) -> None:
    run = svc.start_run(SIM, "g", 5)
    with pytest.raises(InvalidRequestError, match="needs a best_capture_id"):
        svc.finish_run(run.run_id, None, "p")
    with pytest.raises(InvalidRequestError):
        svc.finish_run(run.run_id, None, summary="x" * 2001)
    with pytest.raises(InvalidRequestError):
        svc.finish_run(run.run_id, f"{run.run_id}/0009")


def test_list_and_get_runs(svc: TwoDService) -> None:
    [c] = svc.capture(SIM)
    [summary] = svc.list_runs(SIM)
    assert summary.run_id == c.run_id
    assert svc.get_run(c.run_id).run_id == c.run_id
    with pytest.raises(NotFoundError):
        svc.get_run("20261021-101500_SIM-0001_nope")


# ------------------------------------------------------------- presets
def test_preset_save_apply_readback(svc: TwoDService, sim_light: SimulatedLight) -> None:
    svc.apply(SIM, CameraSettingsPatch(exposure_us=12345, gain_db=4.2, pixel_format="RGB8"))
    svc.set_lights({"white_ring": 30})
    [c] = svc.capture(SIM)
    path = svc.save_preset(SIM, "ring30", c.capture_id, tags={"object": "bin"})
    assert path.name == "ring30.yaml"
    with pytest.raises(AlreadyExistsError):
        svc.save_preset(SIM, "ring30", c.capture_id)
    svc.apply(SIM, CameraSettingsPatch(exposure_us=1000, gain_db=0, pixel_format="Mono8"))
    svc.lights_off()
    res = svc.apply_preset(SIM, "ring30")
    s = res.apply.settings
    assert abs(s.exposure_us - 12345) <= 1 and abs(s.gain_db - 4.2) <= 0.1 + 1e-9
    assert s.pixel_format == "RGB8"
    assert res.lights is not None and sim_light.get_levels()["white_ring"] == 30
    assert [r.name for r in svc.list_presets(SIM)] == ["ring30"]
    assert [r.name for r in svc.list_presets()] == ["ring30"]


def test_preset_errors(svc: TwoDService) -> None:
    [c] = svc.capture(SIM)
    with pytest.raises(NotFoundError):
        svc.get_preset(SIM, "nope")
    with pytest.raises(InvalidRequestError):
        svc.save_preset(SIM, "Bad Name", c.capture_id)
    with pytest.raises(NotFoundError):
        svc.save_preset(SIM, "x", "20261021-101500_SIM-0001_nope/0001")
    with pytest.raises(InvalidRequestError, match="not from camera"):
        svc.save_preset("U330439", "x", c.capture_id)
    with pytest.raises(InvalidRequestError):
        svc.get_preset(SIM, "../x")


# -------------------------------------------------------------- lights
def test_list_set_lights(svc: TwoDService) -> None:
    chans = {c["id"]: c for c in svc.list_lights()}
    assert chans["white_ring"]["max_pct"] == 80 and chans["white_ring"]["level_pct"] == 0
    res = svc.set_lights({"white_ring": 50, "red": 95})
    assert not res.ok
    assert svc.list_lights()[0]["level_pct"] == 50
    assert set(svc.lights_off().values()) == {0.0}


def test_no_light_service(make_service: Callable[..., TwoDService]) -> None:
    from camera_lab.core.safety import load_safety_limits

    svc = TwoDService(load_safety_limits(), None, drivers={})
    with pytest.raises(ConfigurationError):
        svc.list_lights()
    svc.shutdown()


# -------------------------------------------------------------- scoring
def test_score_capture_and_file(svc: TwoDService) -> None:
    [c] = svc.capture(SIM)
    assert svc.score_capture(c.capture_id).to_json() == c.scores.to_json()
    tiff = Path(str(c.preview_path)).parents[1] / "captures" / "0001.tiff"
    assert svc.score_file(tiff).to_json() == c.scores.to_json()
    with pytest.raises(NotFoundError):
        svc.score_capture("20261021-101500_SIM-0001_nope/0001")


def test_score_file_inference_and_errors(svc: TwoDService, tmp_path: Path) -> None:
    p = tmp_path / "plain.tiff"
    tifffile.imwrite(p, np.full((16, 16), 128, np.uint8))
    assert svc.score_file(p).params["target_mean"] == 0.45
    rgb16 = tmp_path / "rgb16.tif"
    tifffile.imwrite(rgb16, np.zeros((8, 8, 3), np.uint16), photometric="rgb")
    assert svc.score_file(rgb16).metric("black_pct") == 100.0
    bad = tmp_path / "f.tiff"
    bad.write_bytes(b"not a tiff")
    for path in (bad, tmp_path / "x.png", tmp_path / "missing.tiff"):
        with pytest.raises(InvalidRequestError):
            svc.score_file(path)
    f32 = tmp_path / "f32.tiff"
    tifffile.imwrite(f32, np.zeros((8, 8), np.float32))
    with pytest.raises(InvalidRequestError):
        svc.score_file(f32)


# -------------------------------------------------------------- misc
def test_limits_view(svc: TwoDService) -> None:
    v = svc.limits_view()
    assert v["limits"]["tuning"]["max_steps_hard"] == 40 and len(v["sha256"]) == 64


def test_build_service(tmp_path: Path) -> None:
    svc = build_service(sim=True)
    assert [i.serial for i in svc.discover()][-1] == SIM
    svc.shutdown()
    with pytest.raises(ConfigurationError):
        build_service(limits_path=tmp_path / "missing.yaml")
    sim = build_service(sim=True, limits_path=tmp_path / "missing.yaml", lights_path=tmp_path / "x")
    assert sim.limits.source == "builtin-sim"
    sim.shutdown()
    with pytest.raises(ConfigurationError):
        build_service(lights_path=tmp_path / "missing.yaml")
    bad = tmp_path / "scoring.yaml"
    bad.write_text("default: {target_mean: 5}\n")
    with pytest.raises(ConfigurationError):
        build_service(sim=True, scoring_path=bad)


def test_shutdown_turns_lights_off(svc: TwoDService, sim_light: SimulatedLight) -> None:
    svc.set_lights({"red": 20})
    svc.shutdown()
    assert sim_light.get_levels()["red"] == 0
    assert not svc.is_open(SIM)


def test_context_manager(make_service: Callable[..., TwoDService]) -> None:
    with make_service() as s:
        s.open(SIM)
    assert not s.is_open(SIM)


def test_readback_warning_uses_ranges_after_apply(svc: TwoDService) -> None:
    """A ROI/format change that moves the camera's limits must not warn spuriously."""
    res = svc.apply(SIM, CameraSettingsPatch(roi=ROI(x=0, y=0, width=256, height=128)))
    assert not any("read-back" in w for w in res.warnings)
