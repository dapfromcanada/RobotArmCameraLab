"""Arena driver unit tests against the fake node map (no hardware, no SDK)."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import numpy as np
import pytest

from camera_lab.core.lights import LightService, SimulatedLight
from camera_lab.core.safety import load_safety_limits
from camera_lab.two_d.camera_config import load_cameras_config
from camera_lab.two_d.drivers import arena as arena_mod
from camera_lab.two_d.drivers.arena import ArenaCamera, ArenaDriver, buffer_to_array
from camera_lab.two_d.drivers.unpack import pack_10p, pack_12p, unpack_10p, unpack_12p
from camera_lab.two_d.errors import (
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

from .fake_arena import FakeBuffer, FakeDevice, FakeSystem, expected_image

SERIAL = "233100505"


@pytest.fixture
def system() -> FakeSystem:
    return FakeSystem()


@pytest.fixture
def cam(system: FakeSystem) -> Iterator[ArenaCamera]:
    c = ArenaDriver(system).open(SERIAL)
    yield c
    c.close()


def _dev(system: FakeSystem) -> FakeDevice:
    return system.created[-1]


def _by(res: Any) -> dict[str, Any]:
    return {r.field: r for r in res.results}


# --------------------------------------------------------------- unpack
@pytest.mark.parametrize("n", [2, 8, 1000])
def test_12p_round_trip(n: int) -> None:
    px = (np.arange(n) * 37 % 4096).astype(np.uint16)
    px[:2] = [0xABC, 0x123]
    packed = pack_12p(px)
    assert packed.size == n * 3 // 2
    assert packed[:3].tolist() == [0xBC, 0x3A, 0x12]  # PFNC LSB-first layout
    assert np.array_equal(unpack_12p(packed, n), px)


@pytest.mark.parametrize("n", [4, 400])
def test_10p_round_trip(n: int) -> None:
    px = (np.arange(n) * 13 % 1024).astype(np.uint16)
    assert np.array_equal(unpack_10p(pack_10p(px), n), px)


def test_unpack_errors() -> None:
    with pytest.raises(ValueError):
        unpack_12p(np.zeros(3, np.uint8), 3)
    with pytest.raises(ValueError):
        unpack_12p(np.zeros(2, np.uint8), 2)
    with pytest.raises(ValueError):
        unpack_10p(np.zeros(5, np.uint8), 3)
    with pytest.raises(ValueError):
        unpack_10p(np.zeros(4, np.uint8), 4)


# ------------------------------------------------------------ discovery
def test_discover_lucid_only_and_no_device_created(system: FakeSystem) -> None:
    [info] = ArenaDriver(system).discover()
    assert (info.serial, info.vendor, info.model, info.firmware, info.ip, info.driver) == (
        SERIAL,
        "Lucid Vision Labs",
        "TRI054S-C",
        "1.138.0.0",
        "192.168.20.229",
        "arena",
    )
    assert system.created == []


def test_vendor_case_insensitive() -> None:
    infos = [{"vendor": "LUCID Vision Labs", "serial": "1", "model": "X"}]
    assert [i.serial for i in ArenaDriver(FakeSystem(infos)).discover()] == ["1"]


def test_never_opens_jai(system: FakeSystem) -> None:
    with pytest.raises(CameraNotFoundError):
        ArenaDriver(system).open("U330439")
    assert system.created == []


def test_open_creates_one_device_and_turns_autos_off(cam: ArenaCamera, system: FakeSystem) -> None:
    [dev] = system.created
    assert dev.info["serial"] == SERIAL
    nm = dev.nodemap
    assert nm["ExposureAuto"].value == "Off"
    assert nm["GainAuto"].value == "Off"
    assert nm["BalanceWhiteAuto"].value == "Off"
    assert nm["TriggerMode"].value == "Off"
    assert dev.tl_stream_nodemap.values == {
        "StreamAutoNegotiatePacketSize": True,
        "StreamPacketResendEnable": True,
        "StreamBufferHandlingMode": "NewestOnly",
    }


def test_close_always_destroys(system: FakeSystem) -> None:
    c = ArenaDriver(system).open(SERIAL)
    c.start_stream()
    c.close()
    c.close()
    assert system.destroyed == [system.created[0]]
    assert not _dev(system).streaming
    with pytest.raises(CameraClosedError):
        c.grab()


def test_open_failure_destroys_device(system: FakeSystem, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(self: ArenaCamera) -> None:
        raise RuntimeError("node map broken")

    monkeypatch.setattr(ArenaCamera, "configure_on_open", boom)
    with pytest.raises(RuntimeError):
        ArenaDriver(system).open(SERIAL)
    assert system.destroyed == system.created


def test_unsupported_initial_format_reset(system: FakeSystem) -> None:
    drv = ArenaDriver(system)
    orig = FakeSystem.create_device

    def create(self: FakeSystem, device_infos: Any = None) -> list[FakeDevice]:
        devs = orig(self, device_infos)
        devs[0].nodemap["PixelFormat"]._value = "YCbCr422_8_CbYCrY"
        return devs

    FakeSystem.create_device = create  # type: ignore[method-assign]
    try:
        c = drv.open(SERIAL)
    finally:
        FakeSystem.create_device = orig  # type: ignore[method-assign]
    assert c.get_settings().pixel_format == "BayerRG8"
    c.close()


def test_sdk_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    import builtins

    real = builtins.__import__

    def fake_import(name: str, *a: Any, **k: Any) -> Any:
        if name.startswith("arena_api"):
            raise ImportError("No module named 'arena_api'")
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(SdkUnavailableError) as ei:
        ArenaDriver()
    assert ei.value.exit_code == 5


# --------------------------------------------------------- capabilities
def test_capabilities_from_node_map(cam: ArenaCamera) -> None:
    caps = cam.capabilities()
    assert caps["exposure_us"].min == 86.264
    assert caps["exposure_us"].max == pytest.approx(1e6 / 0.2 - 500)  # frame rate may be lowered
    assert caps["exposure_us"].increment is None
    assert caps["gain_db"].max == 42.0
    assert "gamma" not in caps  # no Gamma node on the TRI054S-C
    assert caps["wb_red"].max == 256.0
    assert caps["pixel_format"].options == (
        "Mono8",
        "Mono12p",
        "Mono16",
        "BayerRG8",
        "BayerRG12p",
        "BayerRG16",
        "RGB8",
        "RGB12p",
        "RGB16",
        "Mono24",
        "BayerRG24",
    )
    assert caps["roi_width"].increment == 4 and caps["roi_width"].max == 2880
    assert caps["roi_height"].increment == 2 and caps["roi_height"].max == 1860
    assert caps["roi_x"].max == 2876 and caps["roi_x"].increment == 4
    assert all(
        caps[n].requires_stream_stop and caps[n].affects_calibration
        for n in ("roi_x", "roi_y", "roi_width", "roi_height")
    )
    assert caps["trigger_mode"].options == ("off", "software")
    assert caps["frame_rate_hz"].max == 6.558


def test_gamma_when_present() -> None:
    system = FakeSystem(gamma=True)
    c = ArenaDriver(system).open(SERIAL)
    assert "gamma" in c.capabilities()
    res = c.apply(CameraSettingsPatch(gamma=0.5))
    assert res.ok and res.settings.gamma == 0.5
    assert system.created[0].nodemap["GammaEnable"].value is True
    c.close()


@pytest.mark.parametrize("locked_wb", [False, True])
def test_mono_camera_has_no_wb(locked_wb: bool) -> None:
    # locked_wb: TRI089S-M lists the WB nodes but they are not readable (live 2026-10-21)
    c = ArenaDriver(FakeSystem(colour=False, locked_wb=locked_wb)).open(SERIAL)
    assert "wb_red" not in c.capabilities()
    assert c.get_settings().white_balance is None
    res = c.apply(CameraSettingsPatch(white_balance={"red": 1.2}))
    assert _by(res)["white_balance.red"].status == "rejected"
    c.close()


# ---------------------------------------------------------------- apply
def test_apply_and_read_back(cam: ArenaCamera) -> None:
    res = cam.apply(
        CameraSettingsPatch(
            exposure_us=12345.6,
            gain_db=6.0,
            white_balance={"red": 1.5, "blue": 1.3},
            frame_rate_hz=5.0,
            pixel_format="BayerRG8",
            roi=ROI(x=400, y=300, width=1024, height=768),
            trigger_mode="software",
        )
    )
    by = _by(res)
    assert res.ok
    assert by["white_balance.blue"].status == "clamped"  # camera quantises ratios to 1/1024
    assert all(r.status == "applied" for f, r in by.items() if f != "white_balance.blue")
    s = res.settings
    assert s.exposure_us == 12345.6 and s.gain_db == 6.0
    assert s.white_balance == {"red": 1.5, "green": 1.0, "blue": 1.2998046875}
    assert by["white_balance.blue"].value == pytest.approx(1.3, abs=1e-3)
    assert s.pixel_format == "BayerRG8"
    assert s.roi == ROI(x=400, y=300, width=1024, height=768)
    assert s.frame_rate_hz == 5.0
    assert s.trigger_mode == "software"
    assert cam._nm.get_node("BalanceRatioSelector").value == "Red"  # selector restored


def test_apply_rejections(cam: ArenaCamera) -> None:
    res = cam.apply(
        CameraSettingsPatch(
            exposure_us=10.0,
            gamma=1.0,
            pixel_format="YCbCr422_8_CbYCrY",
            roi=ROI(x=2000, y=0, width=1024, height=100),
        )
    )
    by = _by(res)
    assert (
        by["exposure_us"].status == "rejected"
        and "outside camera range" in by["exposure_us"].reason
    )
    assert by["gamma"].status == "rejected" and "not present" in by["gamma"].reason
    assert by["pixel_format"].status == "rejected"
    assert by["roi"].status == "rejected" and "exceeds sensor" in by["roi"].reason


def test_roi_not_on_increment_is_rejected_and_restored(cam: ArenaCamera) -> None:
    before = cam.get_settings().roi
    res = cam.apply(CameraSettingsPatch(roi=ROI(x=3, y=0, width=1024, height=100)))
    assert _by(res)["roi"].status == "rejected"
    assert cam.get_settings().roi == before


def test_frame_rate_not_writable_without_enable(cam: ArenaCamera, system: FakeSystem) -> None:
    res = cam.apply(CameraSettingsPatch(frame_rate_hz=3.0))
    assert res.ok
    assert _dev(system).nodemap["AcquisitionFrameRateEnable"].value is True


def test_wb_rejected_while_auto_on(cam: ArenaCamera, system: FakeSystem) -> None:
    _dev(system).nodemap["BalanceWhiteAuto"]._value = "Continuous"
    res = cam.apply(CameraSettingsPatch(white_balance={"red": 2.0}))
    assert "not writable" in _by(res)["white_balance.red"].reason


def test_format_and_roi_need_stream_stop(cam: ArenaCamera) -> None:
    cam.start_stream()
    with pytest.raises(StreamingStateError, match="pixel_format"):
        cam.apply(CameraSettingsPatch(pixel_format="Mono8"))
    with pytest.raises(StreamingStateError, match="roi"):
        cam.apply(CameraSettingsPatch(roi=ROI(x=4, y=0, width=2876, height=1860)))
    assert cam.apply(CameraSettingsPatch(exposure_us=5000)).ok  # allowed while streaming


def test_node_write_exception_becomes_rejection(
    cam: ArenaCamera, monkeypatch: pytest.MonkeyPatch
) -> None:
    nm = cam._nm
    real_write = type(nm).write

    def write(self: Any, node: Any, v: Any) -> None:
        if node.name == "Gain":
            raise RuntimeError("Arena ERROR: GenICam access denied")
        real_write(self, node, v)

    monkeypatch.setattr(type(nm), "write", write)
    res = cam.apply(CameraSettingsPatch(gain_db=3.0, exposure_us=5000.0))
    assert "access denied" in _by(res)["gain_db"].reason
    assert _by(res)["exposure_us"].status == "applied"


# ----------------------------------------------------------------- grab
@pytest.mark.parametrize(
    ("fmt", "dtype", "shape"),
    [
        ("Mono8", np.uint8, (8, 16)),
        ("Mono12p", np.uint16, (8, 16)),
        ("Mono16", np.uint16, (8, 16)),
        ("BayerRG8", np.uint8, (8, 16)),
        ("BayerRG12p", np.uint16, (8, 16)),
        ("RGB8", np.uint8, (8, 16, 3)),
        ("RGB12p", np.uint16, (8, 16, 3)),
        ("RGB16", np.uint16, (8, 16, 3)),
    ],
)
def test_grab_formats_bit_exact(
    cam: ArenaCamera, system: FakeSystem, fmt: str, dtype: Any, shape: tuple[int, ...]
) -> None:
    cam.apply(CameraSettingsPatch(pixel_format=fmt, roi=ROI(x=0, y=0, width=16, height=8)))
    cam.start_stream()
    f = cam.grab()
    assert f.data.dtype == dtype and f.data.shape == shape
    assert np.array_equal(f.data, expected_image(fmt, 16, 8, f.frame_id))
    assert f.pixel_format == fmt
    assert f.bit_depth == {"8": 8, "p": 12, "6": 16}[fmt[-1]]
    dev = _dev(system)
    assert dev.outstanding == [] and dev.requeued == 1  # requeued


def test_grab_copies_before_requeue(cam: ArenaCamera, system: FakeSystem) -> None:
    cam.apply(CameraSettingsPatch(pixel_format="Mono8", roi=ROI(x=0, y=0, width=16, height=8)))
    cam.start_stream()
    f = cam.grab()
    before = f.data.copy()
    buf = _dev(system).get_buffer()
    ctypes_arr = buf._arr
    for i in range(len(ctypes_arr)):
        ctypes_arr[i] = 0
    assert np.array_equal(f.data, before)


@pytest.mark.parametrize(("in_flight", "first_fresh"), [(0, 4), (2, 6), (5, 9)])
def test_stale_frames_skipped_by_timestamp(
    cam: ArenaCamera, system: FakeSystem, in_flight: int, first_fresh: int
) -> None:
    # fresh = stamped at least SETTLE_PERIODS (2) frame periods after the latched write time
    dev = _dev(system)
    cam.start_stream()
    assert cam.grab().frame_id == 1
    dev.in_flight = in_flight
    cam.apply(CameraSettingsPatch(exposure_us=5000))
    assert cam.grab().frame_id == first_fresh
    assert cam.grab().frame_id == first_fresh + 1  # settled: no more skipping
    assert dev.outstanding == []


def test_flush_fallback_without_timestamp_nodes(cam: ArenaCamera, system: FakeSystem) -> None:
    nm = _dev(system).nodemap
    delattr(nm, "_n_TimestampLatch")
    cam.start_stream()
    assert cam.grab().frame_id == 1
    cam.apply(CameraSettingsPatch(exposure_us=5000))
    assert cam.grab().frame_id == 4  # FLUSH_FRAMES_AFTER_APPLY = 2 stale frames dropped
    assert cam.grab().frame_id == 5
    assert _dev(system).outstanding == []


def test_settle_loop_gives_up_after_max_stale_frames(cam: ArenaCamera, system: FakeSystem) -> None:
    dev = _dev(system)
    cam.start_stream()
    cam.grab()
    dev.in_flight = 100  # latch far ahead of every frame
    cam.apply(CameraSettingsPatch(exposure_us=5000))
    first = dev.frame_id
    f = cam.grab()
    assert f.frame_id == first + 21  # 20 skipped (MAX_STALE_FRAMES), then the next one is used
    assert dev.outstanding == []


def test_software_trigger(cam: ArenaCamera, system: FakeSystem) -> None:
    cam.apply(CameraSettingsPatch(trigger_mode="software"))
    cam.start_stream()
    f = cam.grab()
    assert f.settings.trigger_mode == "software"
    assert _dev(system).nodemap["TriggerSoftware"].executed == 1


def test_grab_errors(cam: ArenaCamera, system: FakeSystem) -> None:
    with pytest.raises(StreamingStateError):
        cam.grab()
    cam.start_stream()
    with pytest.raises(ValueError):
        cam.grab(0)
    dev = _dev(system)
    dev.fault = "timeout"
    with pytest.raises(CaptureTimeoutError) as ei:
        cam.grab(100)
    assert ei.value.code == "capture_timeout"
    dev.fault = "incomplete"
    with pytest.raises(IncompleteFrameError):
        cam.grab()
    assert dev.outstanding == []  # incomplete buffer was requeued


def test_disconnect(cam: ArenaCamera, system: FakeSystem) -> None:
    cam.start_stream()
    _dev(system).disconnected = True
    with pytest.raises(CameraDisconnectedError) as ei:
        cam.grab()
    assert ei.value.code == "camera_disconnected" and ei.value.exit_code == 3
    with pytest.raises(CameraDisconnectedError):
        cam.get_settings()
    cam.close()
    assert system.destroyed  # still destroyed


def test_trigger_not_armed_times_out(cam: ArenaCamera, monkeypatch: pytest.MonkeyPatch) -> None:
    cam.apply(CameraSettingsPatch(trigger_mode="software"))
    cam.start_stream()
    monkeypatch.setattr(
        type(cam._nm),
        "read",
        lambda self, node: False if node.name == "TriggerArmed" else node._value,
    )
    with pytest.raises(CaptureTimeoutError, match="not armed"):
        cam.grab(20)


def test_buffer_to_array_checks_size() -> None:
    buf = FakeBuffer(b"\x00" * 10, 4, 4, 1, False)
    with pytest.raises(IncompleteFrameError):
        buffer_to_array(buf, "Mono8")
    with pytest.raises(ValueError):
        buffer_to_array(FakeBuffer(b"\x00" * 16, 4, 4, 1, False), "YUV9")


# --------------------------------------------------------- via service
def test_through_service(tmp_roots: Any) -> None:
    limits = load_safety_limits()
    light = SimulatedLight()
    system = FakeSystem()
    svc = TwoDService(limits, LightService(light, limits), drivers={"arena": ArenaDriver(system)})
    try:
        assert [c.serial for c in svc.discover()] == [SERIAL]  # JAI skipped
        svc.open(SERIAL, "test")
        view = svc.get_settings(SERIAL)
        # limits policy (schema 2): the camera's own range; working range is guidance
        assert view.ranges["exposure_us"].max.value == pytest.approx(1e6 / 0.2 - 500)
        assert view.ranges["gain_db"].max.value == 42.0
        assert view.working["gain_db"]["max"] == 18
        res = svc.apply(SERIAL, CameraSettingsPatch(gain_db=45.0, exposure_us=8000.0))
        assert not res.ok and res.settings.exposure_us == 8000.0  # gain 45 > camera max 42
        svc.apply(
            SERIAL,
            CameraSettingsPatch(pixel_format="BayerRG8", roi=ROI(x=0, y=0, width=64, height=32)),
        )
        [cap] = svc.capture(SERIAL)
        assert cap.settings.pixel_format == "BayerRG8"
        frame = svc.grab(SERIAL)
        svc.apply(SERIAL, CameraSettingsPatch(pixel_format="Mono12p"))  # service stops the stream
        assert svc.grab(SERIAL).pixel_format == "Mono12p"
        assert frame.data.shape == (32, 64)
    finally:
        svc.shutdown()
    assert system.destroyed == system.created


def test_arena_module_imports_without_sdk() -> None:
    assert arena_mod.ArenaDriver.name == "arena"


def test_discovery_timeout_set(system: FakeSystem) -> None:
    ArenaDriver(system, discovery_timeout_ms=250)
    assert system.DEVICE_INFOS_TIMEOUT_MILLISEC == 250  # type: ignore[attr-defined]


# ---------------------------------------------- exposure ↔ frame rate, HDR
def test_long_exposure_lowers_frame_rate_then_restores(
    cam: ArenaCamera, system: FakeSystem
) -> None:
    nm = _dev(system).nodemap
    assert nm["ExposureTime"].max == pytest.approx(1e6 / 6.558 - 167)
    res = cam.apply(CameraSettingsPatch(exposure_us=180_000))
    assert res.ok and res.settings.exposure_us == 180_000
    assert nm["AcquisitionFrameRateEnable"].value is True
    assert res.settings.frame_rate_hz == pytest.approx(1e6 / 180_500)
    assert any("frame rate lowered" in w for w in res.warnings)
    back = cam.apply(CameraSettingsPatch(exposure_us=10_000))
    assert back.ok and nm["AcquisitionFrameRateEnable"].value is False
    assert any("restored" in w for w in back.warnings)


def test_reapplying_same_format_and_roi_while_streaming(cam: ArenaCamera) -> None:
    # live TRI089S-M: PixelFormat / Width / Height are locked while streaming
    cur = cam.get_settings()
    cam.start_stream()
    res = cam.apply(CameraSettingsPatch(pixel_format=cur.pixel_format, roi=cur.roi))
    assert res.ok, res.results
    assert cam.is_streaming


def test_long_exposure_while_streaming_pauses_stream(cam: ArenaCamera, system: FakeSystem) -> None:
    dev = _dev(system)
    nm = dev.nodemap
    cam.start_stream()
    assert not nm["AcquisitionFrameRateEnable"].is_writable  # locked while streaming (live)
    assert cam.capabilities()["exposure_us"].max == pytest.approx(1e6 / 0.2 - 500)
    res = cam.apply(CameraSettingsPatch(exposure_us=180_000))
    assert res.ok and res.settings.exposure_us == 180_000
    assert res.settings.frame_rate_hz == pytest.approx(1e6 / 180_500)
    assert dev.streaming and dev.start_count == 2 and cam.is_streaming
    back = cam.apply(CameraSettingsPatch(exposure_us=10_000))
    assert back.ok and nm["AcquisitionFrameRateEnable"].value is False
    assert dev.streaming and dev.start_count == 3
    assert cam.grab().settings.exposure_us == 10_000


def test_hdr_channel_long_exposure_while_streaming(cam: ArenaCamera, system: FakeSystem) -> None:
    dev = _dev(system)
    dev.nodemap["HDRTuningEnable"]._value = True
    cam.start_stream()
    keys = [f"hdr.ch{i}.exposure_us" for i in range(2)]
    res = cam.apply(CameraSettingsPatch(advanced=dict.fromkeys(keys, 180_000)))
    assert res.ok, res.results
    assert all(res.settings.advanced[k] == 180_000 for k in keys)
    assert any("frame rate lowered" in w for w in res.warnings)
    assert dev.streaming and cam.is_streaming


def test_hdr_channels_restore_free_run_when_short(cam: ArenaCamera, system: FakeSystem) -> None:
    nm = _dev(system).nodemap
    nm["HDRTuningEnable"]._value = True
    cam.start_stream()
    keys = [f"hdr.ch{i}.exposure_us" for i in range(4)]
    cam.apply(CameraSettingsPatch(advanced=dict.fromkeys(keys, 180_000)))
    assert nm["AcquisitionFrameRateEnable"].value is True
    half = cam.apply(CameraSettingsPatch(advanced={"hdr.ch0.exposure_us": 10_000}))
    assert nm["AcquisitionFrameRateEnable"].value is True  # other channels still long
    assert not any("restored" in w for w in half.warnings)
    back = cam.apply(CameraSettingsPatch(advanced=dict.fromkeys(keys, 10_000)))
    assert nm["AcquisitionFrameRateEnable"].value is False
    assert any("restored" in w for w in back.warnings)
    assert cam.is_streaming and _dev(system).streaming


@pytest.mark.parametrize(("exposure", "free_run"), [(10_000, True), (180_000, False)])
def test_open_restores_free_run_when_exposure_fits(
    cam: ArenaCamera, system: FakeSystem, exposure: float, free_run: bool
) -> None:
    nm = _dev(system).nodemap
    nm["AcquisitionFrameRateEnable"]._value = True  # lowered rate left by an earlier process
    nm["AcquisitionFrameRate"]._value = 5.0
    nm["ExposureTime"]._value = exposure
    cam.configure_on_open()
    assert (nm["AcquisitionFrameRateEnable"].value is False) == free_run
    assert cam._auto_fps is not free_run  # a long exposure restores once it shortens


def test_explicit_frame_rate_while_streaming(cam: ArenaCamera, system: FakeSystem) -> None:
    # the GUI frame-rate slider was rejected ("not writable now") while streaming
    cam.start_stream()
    res = cam.apply(CameraSettingsPatch(frame_rate_hz=3.0))
    assert res.ok, res.results
    assert res.settings.frame_rate_hz == pytest.approx(3.0)
    assert cam.is_streaming and _dev(system).streaming


def test_explicit_frame_rate_wins(cam: ArenaCamera, system: FakeSystem) -> None:
    nm = _dev(system).nodemap
    res = cam.apply(CameraSettingsPatch(frame_rate_hz=2.0, exposure_us=300_000))
    assert res.ok and res.settings.frame_rate_hz == 2.0
    res2 = cam.apply(CameraSettingsPatch(exposure_us=10_000))
    assert res2.ok and nm["AcquisitionFrameRateEnable"].value is True  # not auto, not restored
    too_long = cam.apply(CameraSettingsPatch(frame_rate_hz=5.0, exposure_us=300_000))
    assert {r.field: r.status for r in too_long.results}["exposure_us"] == "rejected"


def test_exposure_beyond_lowest_frame_rate_rejected(cam: ArenaCamera) -> None:
    res = cam.apply(CameraSettingsPatch(exposure_us=6_000_000))
    assert _by(res)["exposure_us"].status == "rejected"


def test_hdr_mode_blocks_exposure(cam: ArenaCamera, system: FakeSystem) -> None:
    _dev(system).nodemap["HDRTuningEnable"]._value = True
    caps = cam.capabilities()
    assert "exposure_us" not in caps and "gain_db" not in caps  # read-only: HDR channels instead
    assert "hdr.ch0.exposure_us" in caps
    res = cam.apply(CameraSettingsPatch(exposure_us=5_000, gain_db=3))
    assert "HDR mode" in _by(res)["exposure_us"].reason
    assert "HDR mode" in _by(res)["gain_db"].reason  # live: Gain is read-only in HDR mode too


def test_long_exposure_through_service(tmp_roots: Any) -> None:
    limits = load_safety_limits()
    system = FakeSystem()
    svc = TwoDService(
        limits, LightService(SimulatedLight(), limits), drivers={"arena": ArenaDriver(system)}
    )
    try:
        svc.open(SERIAL, "test")
        res = svc.apply(SERIAL, CameraSettingsPatch(exposure_us=190_000))
        assert res.ok and res.settings.exposure_us == 190_000
        assert any("frame rate lowered" in w for w in res.warnings)
        assert not any("read-back" in w for w in res.warnings)
        longer = svc.apply(SERIAL, CameraSettingsPatch(exposure_us=250_000))
        assert longer.ok  # no camera_lab cap: only the camera's range limits it
        over = svc.apply(SERIAL, CameraSettingsPatch(exposure_us=6_000_000))
        assert "camera max" in (over.results[0].reason or "")
    finally:
        svc.shutdown()


def test_hdr_mode_no_readback_warning_for_locked_gain(tmp_roots: Any) -> None:
    limits = load_safety_limits()
    system = FakeSystem()
    svc = TwoDService(
        limits, LightService(SimulatedLight(), limits), drivers={"arena": ArenaDriver(system)}
    )
    try:
        svc.open(SERIAL, "test")
        nm = system.created[-1].nodemap
        nm["Gain"]._value = 42.0  # live: core Gain reads 42 dB and is read-only in HDR mode
        nm["HDRTuningEnable"]._value = True
        svc.grab(SERIAL)  # streaming, as during a capture run
        res = svc.apply(SERIAL, CameraSettingsPatch(advanced={"hdr.ch0.exposure_us": 120_000}))
        assert res.ok, res.results
        assert not any("read-back" in w for w in res.warnings), res.warnings
        assert res.settings.advanced["hdr.ch0.exposure_us"] == 120_000
    finally:
        svc.shutdown()


@pytest.mark.parametrize("fmt", ["BayerRG24", "Mono24"])
def test_24bit_linear_hdr_formats(cam: ArenaCamera, system: FakeSystem, fmt: str) -> None:
    """TRI054S raw path (Lucid: BayerRG24, LUT off): 3-byte pixels → uint32, bit depth 24."""
    cam.apply(CameraSettingsPatch(pixel_format=fmt, roi=ROI(x=0, y=0, width=64, height=32)))
    cam.start_stream()
    f = cam.grab()
    assert f.data.dtype == np.uint32 and f.bit_depth == 24
    assert np.array_equal(f.data, expected_image(fmt, 64, 32, f.frame_id))
    assert int(f.data.max()) > 1 << 16  # the top byte is really used


def test_stream_locked_hdr_settings_are_refused_not_ignored(cam: ArenaCamera) -> None:
    """Lucid KB: HDROutput / LUTEnable need the stream stopped; the camera ignored them."""
    cam.start_stream()
    res = cam.apply(
        CameraSettingsPatch(advanced={"hdr.output": "HDRChannel3", "lut.enabled": "off"})
    )
    by = _by(res)
    assert by["advanced.hdr.output"].status == "rejected"
    assert "stop the stream" in (by["advanced.hdr.output"].reason or "")
    assert by["advanced.lut.enabled"].status == "rejected"
    caps = cam.capabilities()
    assert caps["hdr.output"].requires_stream_stop and caps["lut.enabled"].requires_stream_stop
    assert caps["lut.tone_mapping"].options[0] == "Linear"
    cam.stop_stream()
    res = cam.apply(
        CameraSettingsPatch(advanced={"hdr.output": "HDRChannel3", "lut.enabled": "off"})
    )
    assert res.ok and res.settings.advanced["lut.enabled"] == "off"


def test_raw_hdr_mode_through_the_service_while_streaming(tmp_roots: Any) -> None:
    limits = load_safety_limits()
    system = FakeSystem()
    svc = TwoDService(
        limits,
        LightService(SimulatedLight(), limits),
        drivers={"arena": ArenaDriver(system)},
        cameras=load_cameras_config(),
    )
    try:
        svc.open(SERIAL, "test")
        svc.grab(SERIAL)  # streaming: the service stops it for the locked settings
        res = svc.set_mode(SERIAL, "raw_hdr")
        assert res.ok, res.results
        adv = res.settings.advanced
        assert res.settings.pixel_format == "BayerRG24"
        assert (adv["lut.enabled"], adv["color.transformation"], adv["hdr.enabled"]) == (
            "off",
            "off",
            "off",
        )
        [cap] = svc.capture(SERIAL, 1, note="24-bit capture")
        assert cap.preview_path is not None and cap.preview_path.is_file()
        tiff = cap.preview_path.parent.parent / "captures" / "0001.tiff"
        import tifffile

        data = tifffile.imread(tiff)
        assert data.dtype == np.uint32 and int(data.max()) > 1 << 16
        assert 0.0 <= cap.scores.metric("mean_level") <= 1.0
    finally:
        svc.shutdown()


def test_identity_nodes(cam: ArenaCamera) -> None:
    nodes = cam.identity_nodes()
    assert nodes["DeviceFirmwareVersion"] == "1.138.0.0" == cam.info.firmware
    assert nodes["DeviceVersion"] == "K7A4W"
