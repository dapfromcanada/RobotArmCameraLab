"""eBUS driver unit tests against the fake SDK (no hardware), plus real-camera golden data."""

from __future__ import annotations

import math
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from camera_lab.core.lights import LightService, SimulatedLight
from camera_lab.core.safety import load_safety_limits
from camera_lab.two_d.camera_config import load_cameras_config
from camera_lab.two_d.drivers.ebus import EbusCamera, EbusDriver, decode
from camera_lab.two_d.drivers.unpack import (
    pack_rgb10p32,
    pack_rgb10v1,
    pack_rgb12v1,
    unpack_rgb10p32,
    unpack_rgb10v1,
    unpack_rgb12v1,
)
from camera_lab.two_d.errors import (
    CameraBusyError,
    CameraClosedError,
    CameraDisconnectedError,
    CameraNotFoundError,
    CaptureTimeoutError,
    IncompleteFrameError,
    SdkUnavailableError,
    StreamingStateError,
)
from camera_lab.two_d.models import ROI, CameraSettingsPatch
from camera_lab.two_d.service import TwoDService

from .fake_ebus import FakeCamera, FakeSdk

SERIAL = "U330439"
GOLDEN = Path(__file__).parent / "data" / "jai_ap3200_test_patterns_row0.npz"


@pytest.fixture
def sdk() -> FakeSdk:
    return FakeSdk()


@pytest.fixture
def cam(sdk: FakeSdk) -> Iterator[EbusCamera]:
    c = EbusDriver(sdk).open(SERIAL)
    yield c
    c.close()


def _c(sdk: FakeSdk) -> FakeCamera:
    return sdk.connected[-1].cam


def _by(res: Any) -> dict[str, Any]:
    return {r.field: r for r in res.results}


# ------------------------------------------------------- packed formats
@pytest.mark.parametrize("pattern", ["HorizontalColorBar", "GreyHorizontalRamp"])
def test_golden_rows_from_real_camera(pattern: str) -> None:
    """Rows captured from the AP-3200T-PGE test patterns: all formats agree bit-exactly."""
    d = np.load(GOLDEN)
    n = 2064
    rgb8 = d[f"{pattern}_RGB8"].reshape(-1, 3)
    p32 = unpack_rgb10p32(d[f"{pattern}_RGB10p32"], n)
    v1 = unpack_rgb10v1(d[f"{pattern}_RGB10V1Packed"], n)
    v12 = unpack_rgb12v1(d[f"{pattern}_RGB12V1Packed"], n)
    assert np.array_equal(p32 >> 2, rgb8)
    assert np.array_equal(v1, p32)
    assert np.array_equal(v12 >> 2, p32)
    if pattern == "HorizontalColorBar":
        assert len({tuple(px) for px in rgb8}) > 4  # real colours, so channel order is tested


@pytest.mark.parametrize(
    ("pack", "unpack", "bits"),
    [
        (pack_rgb10p32, unpack_rgb10p32, 10),
        (pack_rgb10v1, unpack_rgb10v1, 10),
        (pack_rgb12v1, unpack_rgb12v1, 12),
    ],
)
def test_pack_unpack_round_trip(pack: Any, unpack: Any, bits: int) -> None:
    rgb = (np.arange(16 * 3) * 37 % (1 << bits)).astype(np.uint16).reshape(16, 3)
    assert np.array_equal(unpack(pack(rgb), 16), rgb)


@pytest.mark.parametrize("fn", [unpack_rgb10p32, unpack_rgb10v1, unpack_rgb12v1])
def test_unpack_short_payload(fn: Any) -> None:
    with pytest.raises(ValueError, match="too short"):
        fn(np.zeros(3, np.uint8), 2)


def test_rgb12_needs_even_components() -> None:
    with pytest.raises(ValueError, match="even"):
        unpack_rgb12v1(np.zeros(9, np.uint8), 1)


def test_decode_errors() -> None:
    with pytest.raises(IncompleteFrameError):
        decode(np.zeros(5, np.uint8), "RGB8", 2, 1)
    with pytest.raises(IncompleteFrameError):
        decode(np.zeros(5, np.uint8), "RGB10p32", 2, 1)
    with pytest.raises(ValueError, match="unsupported"):
        decode(np.zeros(16, np.uint8), "Mono8", 2, 1)


# ----------------------------------------------------------- discovery
def test_discover_jai_only(sdk: FakeSdk) -> None:
    [info] = EbusDriver(sdk, discovery_timeout_ms=400).discover()
    assert (info.serial, info.vendor, info.model, info.firmware, info.ip, info.driver) == (
        SERIAL,
        "JAI Corporation",
        "AP-3200T-PGE",
        "0.1.0.7",
        "192.168.10.44",
        "ebus",
    )
    assert sdk.connected == [] and sdk.detection_timeout == 400


def test_never_opens_lucid(sdk: FakeSdk) -> None:
    with pytest.raises(CameraNotFoundError):
        EbusDriver(sdk).open("233100505")
    assert sdk.connected == []


def test_busy_camera(sdk: FakeSdk) -> None:
    sdk.busy = True
    with pytest.raises(CameraBusyError) as ei:
        EbusDriver(sdk).open(SERIAL)
    assert ei.value.exit_code == 3


def test_sdk_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    import builtins

    real = builtins.__import__

    def fake_import(name: str, *a: Any, **k: Any) -> Any:
        if name.startswith("ebus_"):
            raise ImportError("No module named 'ebus_base'")
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(SdkUnavailableError):
        EbusDriver()


# ---------------------------------------------------------------- open
def test_open_configures_camera(cam: EbusCamera, sdk: FakeSdk) -> None:
    c = _c(sdk)
    assert c.v["ExposureAuto"] == "Off" and c.v["BalanceWhiteAuto"] == "Off"
    assert c.v["ExposureMode"] == "Timed" and c.v["IndividualGainMode"] == "Off"
    assert c.v["TestPattern"] == "Off"
    assert c.gain["DigitalRed"] == 1.0 and c.gain["DigitalBlue"] == 1.0
    assert c.trigger["FrameStart"][0] == "Off" and c.trigger["AcquisitionStart"][0] == "Off"
    assert c.v["AcquisitionFrameRate"] == pytest.approx(c.fps_max())  # free-run
    assert sdk.connected[-1].packet == 8944  # negotiated


def test_packet_size_option(sdk: FakeSdk) -> None:
    c = EbusDriver(sdk, packet_size=9000).open(SERIAL)
    assert sdk.connected[-1].packet == 9000
    c.close()


def test_close_releases_everything(sdk: FakeSdk) -> None:
    c = EbusDriver(sdk).open(SERIAL)
    c.start_stream()
    c.close()
    c.close()
    dev, st = sdk.connected[-1], sdk.streams[-1]
    assert st.closed and st in sdk.freed and dev in sdk.freed and dev in sdk.disconnected
    assert not dev.cam.streaming
    with pytest.raises(CameraClosedError):
        c.grab()


def test_open_failure_cleans_up(sdk: FakeSdk, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(self: EbusCamera) -> None:
        raise RuntimeError("configure failed")

    monkeypatch.setattr(EbusCamera, "configure_on_open", boom)
    with pytest.raises(RuntimeError):
        EbusDriver(sdk).open(SERIAL)
    assert sdk.connected[-1] in sdk.disconnected and sdk.streams[-1].closed


# -------------------------------------------------------- capabilities
def test_capabilities(cam: EbusCamera) -> None:
    caps = cam.capabilities()
    assert caps["gain_db"].min == 0.0 and caps["gain_db"].max == pytest.approx(20 * math.log10(8))
    assert (caps["wb_red"].min, caps["wb_red"].max) == (0.47, 4.0)
    assert (caps["wb_green"].min, caps["wb_green"].max) == (1.0, 1.0)
    assert (caps["gamma"].min, caps["gamma"].max) == (0.45, 1.0)
    assert caps["exposure_us"].max == pytest.approx(1e6 / 0.125 - 1000)
    assert caps["pixel_format"].options == ("RGB8", "RGB10V1Packed", "RGB10p32", "RGB12V1Packed")
    assert caps["roi_width"].increment == 16 and caps["roi_x"].increment == 16
    assert caps["roi_height"].increment == 2 and caps["roi_x"].max == 2048
    assert caps["trigger_mode"].requires_stream_stop
    assert not caps["exposure_us"].requires_stream_stop


def test_no_lut_node_means_no_gamma() -> None:
    c = EbusDriver(FakeSdk(lut=False)).open(SERIAL)
    assert "gamma" not in c.capabilities()
    assert _by(c.apply(CameraSettingsPatch(gamma=0.6)))["gamma"].status == "rejected"
    c.close()


# --------------------------------------------------------------- apply
def test_apply_read_back(cam: EbusCamera, sdk: FakeSdk) -> None:
    res = cam.apply(
        CameraSettingsPatch(
            exposure_us=20_000,
            gain_db=6.0,
            gamma=0.6,
            white_balance={"red": 1.3, "blue": 1.1, "green": 1.0},
            roi=ROI(x=512, y=400, width=1024, height=768),
            trigger_mode="software",
        )
    )
    by = _by(res)
    assert res.ok
    assert by["gain_db"].value == pytest.approx(6.0, abs=0.01)
    assert _c(sdk).gain["AnalogAll"] == pytest.approx(
        10 ** (6 / 20), abs=1e-3
    )  # multiplier on the camera
    assert by["white_balance.red"].status == "clamped" and by["white_balance.red"].value == 83 / 64
    assert by["white_balance.green"].status == "applied"
    s = res.settings
    assert s.gamma == 0.6 and _c(sdk).v["LUTMode"] == "Gamma"
    assert s.roi == ROI(x=512, y=400, width=1024, height=768)
    assert s.trigger_mode == "software"
    assert _c(sdk).v["GainSelector"] == "AnalogAll"  # selector restored


def test_gamma_one_is_lut_off(cam: EbusCamera, sdk: FakeSdk) -> None:
    cam.apply(CameraSettingsPatch(gamma=0.5))
    res = cam.apply(CameraSettingsPatch(gamma=1.0))
    assert res.settings.gamma == 1.0 and _c(sdk).v["LUTMode"] == "Off"


def test_apply_rejections(cam: EbusCamera) -> None:
    res = cam.apply(
        CameraSettingsPatch(
            gain_db=30.0,
            white_balance={"green": 1.2, "red": 9.0},
            pixel_format="Mono8",
            roi=ROI(x=2000, y=0, width=1024, height=8),
        )
    )
    by = _by(res)
    assert "outside camera range" in by["gain_db"].reason
    assert "reference" in by["white_balance.green"].reason
    assert by["white_balance.red"].status == "rejected"
    assert by["pixel_format"].status == "rejected"
    assert "exceeds sensor" in by["roi"].reason


def test_roi_off_increment_rejected_and_restored(cam: EbusCamera) -> None:
    before = cam.get_settings().roi
    res = cam.apply(CameraSettingsPatch(roi=ROI(x=8, y=0, width=1024, height=8)))
    assert _by(res)["roi"].status == "rejected"
    assert cam.get_settings().roi == before


def test_streaming_locks(cam: EbusCamera) -> None:
    cam.start_stream()
    for patch in (
        CameraSettingsPatch(pixel_format="RGB10p32"),
        CameraSettingsPatch(roi=ROI(x=0, y=0, width=1024, height=768)),
        CameraSettingsPatch(trigger_mode="software"),
    ):
        with pytest.raises(StreamingStateError):
            cam.apply(patch)
    assert cam.apply(CameraSettingsPatch(exposure_us=5000, gain_db=3, gamma=0.8)).ok


def test_long_exposure_lowers_frame_rate(cam: EbusCamera, sdk: FakeSdk) -> None:
    res = cam.apply(CameraSettingsPatch(exposure_us=150_000))
    assert res.ok and res.settings.exposure_us == 150_000
    assert res.settings.frame_rate_hz == pytest.approx(1e6 / 151_000)
    assert any("lowered" in w for w in res.warnings)
    back = cam.apply(CameraSettingsPatch(exposure_us=10_000))
    assert back.settings.frame_rate_hz == pytest.approx(_c(sdk).fps_max())
    assert any("restored" in w for w in back.warnings)


def test_explicit_frame_rate_wins(cam: EbusCamera) -> None:
    assert cam.apply(CameraSettingsPatch(frame_rate_hz=2.0, exposure_us=300_000)).ok
    res = cam.apply(CameraSettingsPatch(exposure_us=10_000))
    assert res.settings.frame_rate_hz == 2.0
    over = cam.apply(CameraSettingsPatch(exposure_us=600_000))
    assert _by(over)["exposure_us"].status == "rejected"


def test_geometry_change_keeps_free_run(cam: EbusCamera, sdk: FakeSdk) -> None:
    cam.apply(CameraSettingsPatch(roi=ROI(x=0, y=0, width=1024, height=768)))
    assert cam.get_settings().frame_rate_hz == pytest.approx(_c(sdk).fps_max())
    cam.apply(CameraSettingsPatch(pixel_format="RGB12V1Packed"))
    assert cam.get_settings().frame_rate_hz == pytest.approx(_c(sdk).fps_max())


# ---------------------------------------------------------------- grab
@pytest.mark.parametrize(
    ("fmt", "dtype", "bits"),
    [
        ("RGB8", np.uint8, 8),
        ("RGB10p32", np.uint16, 10),
        ("RGB10V1Packed", np.uint16, 10),
        ("RGB12V1Packed", np.uint16, 12),
    ],
)
def test_grab_formats(cam: EbusCamera, sdk: FakeSdk, fmt: str, dtype: Any, bits: int) -> None:
    cam.apply(CameraSettingsPatch(pixel_format=fmt, roi=ROI(x=0, y=0, width=32, height=8)))
    cam.start_stream()
    f = cam.grab()
    assert f.data.shape == (8, 32, 3) and f.data.dtype == dtype and f.bit_depth == bits
    assert np.array_equal(f.data, _c(sdk).image(f.frame_id).astype(dtype))
    assert sdk.streams[-1].pipelines[-1].outstanding == []


def test_grab_drains_stale_frames(cam: EbusCamera, sdk: FakeSdk) -> None:
    cam.apply(CameraSettingsPatch(roi=ROI(x=0, y=0, width=32, height=8)))
    cam.start_stream()
    pipeline = sdk.streams[-1].pipelines[-1]
    pipeline.backlog = 3
    assert cam.grab().frame_id == 4  # three queued frames dropped
    cam.apply(CameraSettingsPatch(exposure_us=5000))
    assert cam.grab().frame_id == 7  # two more dropped after the apply


def test_software_trigger(cam: EbusCamera, sdk: FakeSdk) -> None:
    cam.apply(CameraSettingsPatch(trigger_mode="software", roi=ROI(x=0, y=0, width=32, height=8)))
    cam.start_stream()
    assert cam.grab().settings.trigger_mode == "software"


def test_grab_errors(cam: EbusCamera, sdk: FakeSdk) -> None:
    with pytest.raises(StreamingStateError):
        cam.grab()
    cam.apply(CameraSettingsPatch(roi=ROI(x=0, y=0, width=32, height=8)))
    cam.start_stream()
    with pytest.raises(ValueError):
        cam.grab(0)
    _c(sdk).fault = "timeout"
    with pytest.raises(CaptureTimeoutError):
        cam.grab(100)
    _c(sdk).fault = "incomplete"
    with pytest.raises(IncompleteFrameError, match="MISSING_PACKETS"):
        cam.grab()
    assert sdk.streams[-1].pipelines[-1].outstanding == []


def test_disconnect(cam: EbusCamera, sdk: FakeSdk) -> None:
    cam.apply(CameraSettingsPatch(roi=ROI(x=0, y=0, width=32, height=8)))
    cam.start_stream()
    _c(sdk).connected = False
    with pytest.raises(CameraDisconnectedError):
        cam.grab()
    with pytest.raises(CameraDisconnectedError):
        cam.get_settings()


def test_stream_restart_after_format_change(cam: EbusCamera, sdk: FakeSdk) -> None:
    cam.apply(CameraSettingsPatch(roi=ROI(x=0, y=0, width=32, height=8)))
    cam.start_stream()
    cam.stop_stream()
    cam.apply(CameraSettingsPatch(pixel_format="RGB10p32"))
    cam.start_stream()
    pl = sdk.streams[-1].pipelines[-1]
    assert pl.size == 32 * 8 * 4  # pipeline rebuilt with the new payload size
    assert cam.grab().pixel_format == "RGB10p32"


# ----------------------------------------------------------- service
def test_through_service(tmp_roots: Any) -> None:
    limits = load_safety_limits()
    sdk = FakeSdk()
    svc = TwoDService(
        limits,
        LightService(SimulatedLight(), limits),
        drivers={"ebus": EbusDriver(sdk)},
        cameras=load_cameras_config(),
    )
    try:
        assert [c.serial for c in svc.discover()] == [SERIAL]
        svc.open("apex", "test")  # alias from cameras.yaml
        view = svc.get_settings(SERIAL)
        assert view.ranges["gain_db"].max.value == pytest.approx(18.06, abs=0.01)  # camera ×8
        assert view.ranges["exposure_us"].max.value == pytest.approx(7_999_000)  # camera range
        res = svc.apply(
            SERIAL,
            CameraSettingsPatch(
                gain_db=12.0, exposure_us=180_000, roi=ROI(x=0, y=0, width=64, height=32)
            ),
        )
        assert res.ok and any("lowered" in w for w in res.warnings)
        assert not any("read-back" in w for w in res.warnings)
        [cap] = svc.capture(SERIAL)
        assert cap.settings.pixel_format == "RGB8"
        svc.apply(SERIAL, CameraSettingsPatch(pixel_format="RGB12V1Packed"))
        f = svc.grab(SERIAL)
        assert f.bit_depth == 12 and f.data.shape == (32, 64, 3)
    finally:
        svc.shutdown()
    assert sdk.connected[-1] in sdk.disconnected


def test_firmware_is_device_firmware_version_not_discovery_version(cam: EbusCamera) -> None:
    # live AP-3200T-PGE 2026-10-22: discovery "version" 0.1.0.7 is DeviceVersion
    assert cam.info.firmware == "0.0.3.4"
    nodes = cam.identity_nodes()
    assert nodes["DeviceFirmwareVersion"] == "0.0.3.4"
    assert nodes["DeviceVersion"] == "0.1.0.7"
