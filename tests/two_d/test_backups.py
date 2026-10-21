"""Phase 10 step 4: camera settings backup and restore."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from camera_lab.cli import app
from camera_lab.core.lights import LightService, SimulatedLight
from camera_lab.core.safety import load_safety_limits
from camera_lab.two_d.backups import (
    BackupError,
    BackupStore,
    FeatureAccess,
    FeatureValue,
    multipass_restore,
)
from camera_lab.two_d.camera_config import load_cameras_config
from camera_lab.two_d.drivers.arena import ArenaDriver
from camera_lab.two_d.drivers.ebus import EbusDriver
from camera_lab.two_d.errors import DeviceError, NotFoundError
from camera_lab.two_d.models import CameraSettingsPatch
from camera_lab.two_d.service import TwoDService

from .fake_arena import FakeSystem
from .fake_ebus import FakeSdk

SIM = "SIM-0001"
LUCID = "233100505"
JAI = "U330439"


def _service(drivers: dict[str, Any], root: Path) -> TwoDService:
    limits = load_safety_limits()
    return TwoDService(
        limits,
        LightService(SimulatedLight(), limits),
        drivers=drivers,  # type: ignore[arg-type]
        cameras=load_cameras_config(),
        backups_root=root,
    )


# ------------------------------------------------------------------ store
def test_store_save_dedupe_list_load(tmp_path: Path) -> None:
    t = iter([datetime(2026, 10, 21, 9, 0, 0, tzinfo=UTC)] * 10)
    store = BackupStore(tmp_path, now=lambda: next(t))
    a, created = store.save(serial=SIM, driver="simulated", payload={"x": 1}, reason="first")
    assert created and a.backup_id == "20261021T090000Z"
    same, created = store.save(serial=SIM, driver="simulated", payload={"x": 1}, reason="again")
    assert not created and same.backup_id == a.backup_id  # identical → stored once
    b, created = store.save(serial=SIM, driver="simulated", payload={"x": 2}, reason="changed")
    assert (
        created and b.backup_id == "20261021T090000Z-1"
    )  # same second → suffix, never overwritten
    assert [x.backup_id for x in store.list(SIM)] == [a.backup_id, b.backup_id]
    assert store.load(SIM, a.backup_id).payload == {"x": 1}


def test_store_rejects_bad_ids_and_tampering(tmp_path: Path) -> None:
    store = BackupStore(tmp_path)
    b, _ = store.save(serial=SIM, driver="simulated", payload={"x": 1}, reason="r")
    for bad in ("../x", "20261021", "x" * 40):
        with pytest.raises(BackupError, match="invalid backup id"):
            store.load(SIM, bad)
    with pytest.raises(BackupError, match="no backup"):
        store.load(SIM, "20201010T101010Z")
    with pytest.raises(ValueError):
        store.list("../evil")
    path = tmp_path / SIM / f"{b.backup_id}.json"
    data = json.loads(path.read_text())
    data["payload"]["x"] = 99  # edited by hand
    path.write_text(json.dumps(data))
    with pytest.raises(BackupError, match="checksum"):
        store.load(SIM, b.backup_id)
    other = tmp_path / "OTHER-1"
    other.mkdir()
    (other / f"{b.backup_id}.json").write_text(json.dumps({**data, "payload": {"x": 1}}))
    with pytest.raises(BackupError, match="does not belong"):
        store.load("OTHER-1", b.backup_id)


# -------------------------------------------------------------- multipass
class _Dict(FeatureAccess):
    """'Exposure' is writable only while 'Hdr' is off; 'Gain' is per 'Sel' entry."""

    def __init__(self) -> None:
        self.v: dict[str, Any] = {"Hdr": True, "Exposure": 100.0, "Sel": "A", "Locked": 1}
        self.gain = {"A": 1.0, "B": 2.0}
        self.writes: list[str] = []

    def select(self, selector: str, value: str | int) -> None:
        self.v[selector] = value

    def read(self, name: str) -> Any:
        return self.gain[self.v["Sel"]] if name == "Gain" else self.v[name]

    def writable(self, name: str) -> bool:
        return not (name == "Exposure" and self.v["Hdr"]) and name != "Locked"

    def write(self, name: str, value: Any) -> None:
        self.writes.append(name)
        if name == "Gain":
            self.gain[self.v["Sel"]] = value
        else:
            self.v[name] = value


def test_multipass_resolves_order_dependencies_and_selectors() -> None:
    acc = _Dict()
    feats = [
        FeatureValue(name="Exposure", value=500.0, writable=True),  # needs Hdr off first
        FeatureValue(name="Hdr", value=False, writable=True),
        FeatureValue(name="Sel", value="A", writable=True),
        FeatureValue(name="Gain", value=4.0, writable=True, selector="Sel", selector_value="B"),
        FeatureValue(name="Locked", value=7, writable=False),  # informational only
    ]
    r = multipass_restore(feats, acc)
    assert r.ok, (r.failed, r.differences)
    assert acc.v["Exposure"] == 500.0 and acc.v["Hdr"] is False
    assert acc.gain == {"A": 1.0, "B": 4.0} and acc.v["Sel"] == "A"  # selector restored
    assert r.informational == 1 and "Locked" not in acc.writes


def test_multipass_reports_what_cannot_be_set() -> None:
    acc = _Dict()
    feats = [FeatureValue(name="Exposure", value=500.0, writable=True)]  # Hdr stays on
    r = multipass_restore(feats, acc)
    assert not r.ok
    assert r.failed[0]["feature"] == "Exposure" and "not writable" in r.failed[0]["reason"]
    assert r.differences == ("Exposure: backup 500.0, camera 100.0",)


# ---------------------------------------------------------------- service
def test_first_change_is_backed_up_once(
    make_service: Callable[..., TwoDService], tmp_path: Path
) -> None:
    svc = make_service(backups_root=tmp_path)
    svc.open(SIM, "test")
    assert svc.list_backups(SIM) == []  # opening the simulator changes nothing
    before = svc.get_settings(SIM).settings.exposure_us
    svc.apply(SIM, CameraSettingsPatch(exposure_us=before * 2))
    svc.apply(SIM, CameraSettingsPatch(exposure_us=before * 3))
    [b] = svc.list_backups(SIM)
    assert "first settings change" in b.reason
    assert b.settings["exposure_us"] == pytest.approx(before)


def test_change_refused_when_backup_fails(
    make_service: Callable[..., TwoDService], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc = make_service(backups_root=tmp_path)
    svc.open(SIM, "test")
    cam = svc._open[SIM].camera  # type: ignore[attr-defined]

    def broken() -> dict[str, Any]:
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(cam, "backup_settings", broken)
    before = svc.get_settings(SIM).settings.exposure_us
    with pytest.raises(DeviceError, match="camera not changed"):
        svc.apply(SIM, CameraSettingsPatch(exposure_us=before * 2))
    assert svc.get_settings(SIM).settings.exposure_us == before


def test_restore_simulated_and_camera_range(
    make_service: Callable[..., TwoDService], tmp_path: Path
) -> None:
    svc = make_service(backups_root=tmp_path)
    svc.open(SIM, "test")
    start = svc.get_settings(SIM).settings
    b, _ = svc.backup(SIM, "test")
    svc.apply(SIM, CameraSettingsPatch(exposure_us=start.exposure_us * 2, gain_db=6.0))
    report, before_id = svc.restore_backup(SIM, b.backup_id)
    assert report.ok, report
    now = svc.get_settings(SIM).settings
    assert now.exposure_us == pytest.approx(start.exposure_us) and now.gain_db == start.gain_db
    assert before_id != b.backup_id and len(svc.list_backups(SIM)) == 2  # undo point kept

    # beyond the camera's own range: the camera rejects it and the report says so
    over = {**b.payload, "settings": {**b.payload["settings"], "exposure_us": 5_000_000.0}}
    bad, _ = BackupStore(tmp_path).save(serial=SIM, driver="simulated", payload=over, reason="x")
    report, _ = svc.restore_backup(SIM, bad.backup_id)
    assert not report.ok and report.failed[0]["feature"] == "exposure_us"
    assert svc.get_settings(SIM).settings.exposure_us == pytest.approx(start.exposure_us)
    with pytest.raises(NotFoundError):
        svc.restore_backup(SIM, "20201010T101010Z")


def test_arena_open_backup_and_restore(tmp_path: Path) -> None:
    system = FakeSystem(hdr=False)
    svc = _service({"arena": ArenaDriver(system)}, tmp_path)
    try:
        svc.open(LUCID, "test")
        [b] = svc.list_backups(LUCID)
        assert "ExposureAuto Continuous -> Off" in b.reason  # backed up before configuring
        assert (
            b.payload["format"] == "arena-v1"
            and "ExposureAuto\tContinuous" in b.payload["streamables"]
        )
        names = {f["name"] for f in b.payload["supplement"]}
        assert {"HDRTuningEnable", "ExposureTime", "Gain"} <= names
        start = svc.get_settings(LUCID).settings
        svc.grab(LUCID)  # streaming, as in a session
        svc.apply(LUCID, CameraSettingsPatch(exposure_us=12_345.0, gain_db=6.0))
        report, _ = svc.restore_backup(LUCID, b.backup_id)
        assert report.ok, (report.failed, report.differences)
        nm = system.created[-1].nodemap
        assert nm["ExposureAuto"].value == "Continuous"  # as before camera_lab touched it
        assert nm["Gain"].value == pytest.approx(start.gain_db)
        assert not svc._open[LUCID].camera.is_streaming  # type: ignore[attr-defined]
    finally:
        svc.shutdown()


def test_arena_backup_while_streaming_pauses_the_stream(tmp_path: Path) -> None:
    system = FakeSystem(hdr=False)
    svc = _service({"arena": ArenaDriver(system)}, tmp_path)
    try:
        svc.open(LUCID, "test")
        svc.grab(LUCID)  # streaming
        dev = system.created[-1]
        starts = dev.start_count
        b, created = svc.backup(LUCID, "while streaming")
        assert created and "PixelFormat" in b.payload["streamables"]
        assert dev.streaming and dev.start_count == starts + 1  # paused and restarted
        assert svc.grab(LUCID).data is not None
    finally:
        svc.shutdown()


def test_arena_restore_hdr_channels(tmp_path: Path) -> None:
    system = FakeSystem()
    svc = _service({"arena": ArenaDriver(system)}, tmp_path)
    try:
        svc.open(LUCID, "test")
        nm = system.created[-1].nodemap
        nm["HDRTuningEnable"]._value = True
        nm.hdr_gain = [12.0, 4.2, 15.0, 4.2]  # within the 18 dB limit
        b, _ = svc.backup(LUCID, "hdr")
        keys = [f"hdr.ch{i}.exposure_us" for i in range(4)]
        svc.apply(LUCID, CameraSettingsPatch(advanced=dict.fromkeys(keys, 20_000.0)))
        report, _ = svc.restore_backup(LUCID, b.backup_id)
        assert report.ok, (report.failed, report.differences)
        assert system.created[-1].nodemap.hdr_exposure == [10000.472] * 4
    finally:
        svc.shutdown()


def test_ebus_backup_walk_and_restore(tmp_path: Path) -> None:
    sdk = FakeSdk()
    svc = _service({"ebus": EbusDriver(sdk)}, tmp_path)
    try:
        svc.open(JAI, "test")
        [b] = svc.list_backups(JAI)
        feats = {(f["name"], f.get("selector_value")): f["value"] for f in b.payload["features"]}
        assert feats[("ExposureAuto", None)] == "Continuous"  # before camera_lab turned it off
        assert feats[("Gain", "DigitalRed")] == "1.05"  # selector expanded
        assert feats[("ExposureTime", "Red")] == "20164.0"
        svc.apply(JAI, CameraSettingsPatch(gain_db=6.0, exposure_us=30_000.0))
        report, _ = svc.restore_backup(JAI, b.backup_id)
        assert report.ok, (report.failed, report.differences)
        cam = sdk.connected[-1].cam
        assert cam.v["ExposureAuto"] == "Continuous" and cam.gain["DigitalRed"] == 1.05
        assert cam.gain["AnalogAll"] == 1.0
    finally:
        svc.shutdown()


# -------------------------------------------------------------------- cli
def test_cli_backup_list_restore(tmp_roots: Path) -> None:
    runner = CliRunner()
    res = runner.invoke(app, ["--sim", "two-d", "backup", "sim", "--json"])
    assert res.exit_code == 0, res.output
    bid = json.loads(res.stdout)["result"]["backup_id"]
    assert tmp_roots.joinpath("config", "camera_backups", SIM, f"{bid}.json").is_file()
    res = runner.invoke(app, ["--sim", "two-d", "backups", "sim"])
    assert res.exit_code == 0 and bid in res.stdout
    res = runner.invoke(app, ["--sim", "two-d", "restore", "sim", bid], input="n\n")
    assert res.exit_code == 2  # declined
    res = runner.invoke(app, ["--sim", "two-d", "restore", "sim", bid, "--yes", "--json"])
    assert res.exit_code == 0, res.output
    assert json.loads(res.stdout)["result"]["ok"] is True
    res = runner.invoke(app, ["--sim", "two-d", "restore", "sim", "../../etc", "--yes"])
    assert res.exit_code == 2


def test_arena_restore_hdr_gains_above_the_old_caps(tmp_path: Path) -> None:
    """Live TRI054S-C HDR channel gains 19.8 / 29.4 dB: refused under schema 1, restored now."""
    system = FakeSystem()
    svc = _service({"arena": ArenaDriver(system)}, tmp_path)
    try:
        svc.open(LUCID, "test")
        [b] = svc.list_backups(LUCID)
        chans = {
            f["selector_value"]
            for f in b.payload["supplement"]
            if f["name"] == "HDRChannelAnalogGain"
        }
        assert chans == {0, 1, 2, 3}  # HDR channels backed up (not in the SDK streamables)
        nm = system.created[-1].nodemap
        nm.hdr_gain = [1.0, 1.0, 1.0, 1.0]
        report, _ = svc.restore_backup(LUCID, b.backup_id)
        assert report.ok, (report.failed, report.differences)
        assert nm.hdr_gain == [19.8, 4.2, 29.4, 4.2]
    finally:
        svc.shutdown()


def test_restore_skips_protected_features(tmp_path: Path) -> None:
    data = yaml.safe_load(Path(load_safety_limits().source).read_text())
    data["cameras"][LUCID] = {"protected_features": ["PixelFormat", "GevPersistent*"]}
    limits_file = tmp_path / "safety_limits.yaml"
    limits_file.write_text(yaml.safe_dump(data))
    limits = load_safety_limits(limits_file)
    system = FakeSystem(hdr=False)
    svc = TwoDService(
        limits,
        LightService(SimulatedLight(), limits),
        drivers={"arena": ArenaDriver(system)},
        cameras=load_cameras_config(),
        backups_root=tmp_path / "backups",
    )
    try:
        svc.open(LUCID, "test")
        b, _ = svc.backup(LUCID, "before")
        assert "PixelFormat\tRGB8" in b.payload["streamables"]
        svc.apply(LUCID, CameraSettingsPatch(pixel_format="BayerRG8", exposure_us=12_345.0))
        report, _ = svc.restore_backup(LUCID, b.backup_id)
        nm = system.created[-1].nodemap
        assert nm["PixelFormat"].value == "BayerRG8"  # protected: not written back
        assert report.protected_skipped == ("PixelFormat: backup 'RGB8', camera 'BayerRG8'",)
        assert nm["ExposureTime"].value != 12_345.0  # the rest was restored
    finally:
        svc.shutdown()
