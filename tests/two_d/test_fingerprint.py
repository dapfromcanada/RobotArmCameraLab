"""Phase 11: the fingerprint recovers a simulated sensor's known properties."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import ClassVar

import numpy as np
import pytest
import yaml

from camera_lab.two_d.drivers.simulated import SensorModel, SimulatedCameraDriver
from camera_lab.two_d.errors import InvalidRequestError
from camera_lab.two_d.fingerprint import measure as fm
from camera_lab.two_d.fingerprint.session import FingerprintSession, FpResult
from camera_lab.two_d.models import CameraSettingsPatch
from camera_lab.two_d.service import TwoDService

SIM = "SIM-0001"
SENSOR = SensorModel(dark_current_e_s=200, prnu=0.01, vignetting=0.3, hot_pixels=20)
K = 4095 / 10_000  # Mono12 DN per electron at 0 dB


@pytest.fixture
def svc(make_service: Callable[..., TwoDService], tmp_path: Path) -> TwoDService:
    drv = SimulatedCameraDriver(None, sensor=SENSOR, black_level=0.05)
    s = make_service(drivers={"simulated": drv}, backups_root=tmp_path / "backups")
    s.open(SIM, "test")
    s.apply(SIM, CameraSettingsPatch(pixel_format="Mono8"))  # a mono sensor: Mono12 is raw
    return s


def _scene(svc: TwoDService, kind: str) -> None:
    svc._open[SIM].camera.scene = kind  # type: ignore[attr-defined]  # the human's action


def test_fingerprint_recovers_the_sensor(svc: TwoDService, tmp_path: Path) -> None:
    ses = FingerprintSession(svc, SIM, root=tmp_path / "fp", reset=False)
    before = svc.get_settings(SIM).settings
    ident = ses.step_identity()
    assert ident.ok and ses.fmt == "Mono12"
    assert ses.step_timing().data["stale_frames_after_change"] == 0
    assert ses.step_dark().status == "fail"  # chart in view: "put the lens cap on"
    _scene(svc, "cap")
    dark = ses.step_dark()
    assert dark.ok, dark.message
    view = svc.get_settings(SIM).settings  # back to a brightness the human can see by
    assert (view.exposure_us, view.gain_db) == (before.exposure_us, before.gain_db)
    d = dark.data
    assert d["black_dn"] == pytest.approx(0.05 * 4095, abs=1.0)
    assert d["read_noise_dn"] / K == pytest.approx(5.0, rel=0.05)
    assert d["dark_current_dn_s"] / K == pytest.approx(200, rel=0.05)
    assert d["hot_pixel_count"] == 20
    _scene(svc, "chart")
    flat_fail = ses.step_flat()
    assert flat_fail.status == "fail" and "diffuser" in flat_fail.message
    _scene(svc, "flat")
    flat = ses.step_flat()
    assert flat.ok, flat.message
    ptc = flat.data["photon_transfer"]
    assert ptc["system_gain_dn_per_e"] == pytest.approx(K, rel=0.02)
    assert ptc["read_noise_e"] == pytest.approx(5.0, rel=0.15)
    assert ptc["full_well_e"] == pytest.approx(10_000 * 0.95, rel=0.03)  # above the 5 % pedestal
    assert ptc["linearity_error_pct"] < 0.5
    assert flat.data["flat"]["prnu_pct"] == pytest.approx(1.0, rel=0.1)
    ideal = fm.flat_result(svc._open[SIM].camera._pixel_gain * 1000 + 1, 1, 0, 8)  # type: ignore[attr-defined]
    assert flat.data["flat"]["vignetting_corner_ratio"] == pytest.approx(
        ideal.vignetting_corner_ratio, abs=0.01
    )
    assert ses.step_colour().status == "skipped"  # Mono8 on the sim: no colour planes
    assert ses.step_stage(0).status == "skipped"  # not an HDR camera
    assert ses.step_light_back().status == "skipped"  # no channel stage changed the light
    noise = ses.step_noise_gain()
    table = noise.data["table"]
    assert [r["gain_db"] for r in table][:3] == [0.0, 3.0, 6.0]
    snr = [r["snr"] for r in table]
    assert snr == sorted(snr, reverse=True)  # this sensor has no gain-mode switch
    assert noise.data["snr_rises_at_db"] == []
    assert table[0]["gain_dn_per_e"] == pytest.approx(K, rel=0.1)  # photon transfer per gain
    assert table[0]["read_noise_e"] == pytest.approx(5.0, rel=0.2)
    cam = svc._open[SIM].camera
    cam.iris_transmission = 0.5  # type: ignore[attr-defined]  # one stop only: STOP
    stop = ses.step_lens_iris()
    assert stop.status == "fail" and "close the iris" in stop.message
    cam.iris_transmission = 0.25  # type: ignore[attr-defined]  # two f-stops closed
    lens = ses.step_lens_iris()
    assert lens.ok and lens.data["light_vs_diffuser_row"] == pytest.approx(0.25, rel=0.05)
    assert ses.step_light_back().status == "fail"  # iris still closed
    cam.iris_transmission = 1.0  # type: ignore[attr-defined]
    assert ses.step_light_back().ok
    assert ses.step_user_sets().ok
    rep = ses.step_report()
    assert rep.ok and "camera restored" in rep.message
    folder = Path(rep.data["folder"])
    doc = yaml.safe_load((folder / "fingerprint.yaml").read_text())
    assert doc["dark"]["hot_pixel_count"] == 20 and "photon_transfer" in doc["flat"]
    assert (folder / "master_dark.npz").is_file() and (folder / "master_flat.npz").is_file()
    report = (folder / "report.md").read_text()
    assert "system gain" in report
    assert doc["camera"] == {
        "model": doc["identity"]["model"],
        "serial": SIM,
        "firmware": "sim-1",
        "vendor": doc["identity"]["vendor"],
    }
    assert list(doc)[:2] == ["schema_version", "camera"]  # identity first in the file
    assert f"**Serial:** {SIM}" in report and "**Firmware:** sim-1" in report
    after = svc.get_settings(SIM).settings
    assert after.pixel_format == before.pixel_format and after.exposure_us == before.exposure_us


def test_order_is_enforced(svc: TwoDService, tmp_path: Path) -> None:
    ses = FingerprintSession(svc, SIM, root=tmp_path / "fp", reset=False)
    with pytest.raises(InvalidRequestError, match="identity"):
        ses.step_dark()
    ses.step_identity()
    with pytest.raises(InvalidRequestError, match="dark"):
        ses.step_flat()


def test_gain_mode_switch_is_flagged() -> None:
    rows = [{"snr": 100}, {"snr": 80}, {"snr": 95}, {"snr": 70}]
    switches = [i for i in range(1, len(rows)) if rows[i]["snr"] > 1.05 * rows[i - 1]["snr"]]
    assert switches == [2]  # mirrors step_noise_gain's rule on a TRI054S-like table
    assert np.isfinite(rows[0]["snr"])


def test_format_family() -> None:
    from camera_lab.two_d.fingerprint.session import format_family

    assert format_family("RGB8") == format_family("RGB12V1Packed") == "RGB"
    assert format_family("BayerRG8") == format_family("BayerRG12p") == "BayerRG"
    assert format_family("Mono8") == "Mono" != format_family("BayerRG12p")


# ------------------------------------------------------------- HDR camera
TRI054S = "233100505"


def _tri054s(tmp_roots: Path) -> TwoDService:
    from camera_lab.core.lights import LightService, SimulatedLight
    from camera_lab.core.safety import load_safety_limits
    from camera_lab.two_d.camera_config import load_cameras_config
    from camera_lab.two_d.drivers.arena import ArenaDriver

    from .fake_arena import FakeSystem

    limits = load_safety_limits()
    return TwoDService(
        limits,
        LightService(SimulatedLight(), limits),
        drivers={"arena": ArenaDriver(FakeSystem())},
        cameras=load_cameras_config(),
    )


def test_hdr_identity_uses_the_design_mode(tmp_roots: Path) -> None:
    svc = _tri054s(tmp_roots)
    try:
        svc.open(TRI054S, "test")
        ses = FingerprintSession(svc, TRI054S, root=tmp_roots / "fp")
        ident = ses.step_identity()
        assert ident.ok, ident.message
        assert (ses.mode, ses.fmt, ses.channel_fmt) == ("raw_hdr", "BayerRG24", "BayerRG12p")
        assert ses.hdr_channels == tuple(f"HDRChannel{i}" for i in range(4))
        assert not ses.gain_knob  # raw_hdr knobs: exposure only
        adv = svc.get_settings(TRI054S).settings.advanced
        assert adv["black.level"] == pytest.approx(5.0)  # off 0, so darks are not clipped
        assert ident.data["black_level_raised_to"] == pytest.approx(5.0)
        assert ses.full_scale == float((1 << 24) - 1)
        assert (ident.data["firmware"], ident.data["device_version"]) == ("1.138.0.0", "K7A4W")
        assert "firmware 1.138.0.0" in ident.message
    finally:
        svc.shutdown()


def _discovery(ses: FingerprintSession) -> None:
    """A camera model without a light recipe yet."""
    ses.recipe = None
    ses.lens = ""
    ses.plan = ses._build_plan()


def test_hdr_channels_discovery_says_how_much_light(
    tmp_roots: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No recipe: photon model per channel in place of the fake's light-blind image."""
    svc = _tri054s(tmp_roots)
    try:
        svc.open(TRI054S, "test")
        ses = FingerprintSession(svc, TRI054S, root=tmp_roots / "fp")
        _discovery(ses)
        ses.min_exposure_us = 0.0  # stage logic under test, not the bench's exposure floor
        assert ses.plan["light_2"].label == "3. HDR channels (discovery)"
        assert ses.plan["light_2"].can_skip and not ses.plan["light_3"].active
        assert ses.step_identity().ok
        ses.results["flat"] = FpResult(step="flat", status="pass", message="")  # diffuser on
        black = 41.0
        for out in ses.hdr_channels:
            ses._channel_black[out] = fm.DarkPoint(
                gain_db=0, exposure_us=1e4, black_dn=black, read_noise_dn=2.0, dsnu_dn=0.5
            )
        # DN/µs per channel: ch0 far too sensitive for this light, ch3 the least
        light = {"level": 1.0}
        sens = {"HDRChannel0": 400.0, "HDRChannel1": 2.0, "HDRChannel2": 3.0, "HDRChannel3": 0.3}
        k_dn_e = {"HDRChannel0": 0.8, "HDRChannel1": 0.2, "HDRChannel2": 1.6, "HDRChannel3": 0.1}
        rng = np.random.default_rng(1)

        def plane() -> np.ndarray:
            s = svc.get_settings(TRI054S).settings
            out = str(s.advanced["hdr.output"])
            assert s.pixel_format == ("BayerRG12p" if out != "HDR" else "BayerRG24")
            sig = sens[out] * light["level"] * s.exposure_us
            var = k_dn_e[out] * sig + 4.0
            img = black + sig + rng.normal(0, np.sqrt(var), (200, 200))
            return np.clip(img, 0, 4095)

        monkeypatch.setattr(ses, "_plane", plane)
        ses.set_iris(1.4)  # discovery: the human picks the stop they set
        first = ses.step_stage(0)
        assert first.status == "fail"
        assert list(first.data["problems"]) == ["HDRChannel0"]
        assert "needs more than 10× less light" in first.message
        assert "Change the light as asked" in first.message
        assert set(first.data["channels"]) == {"HDRChannel1", "HDRChannel2", "HDRChannel3"}
        assert first.data["channels"]["HDRChannel3"]["lens"] == "Kowa 8 mm, f/1.4"
        settings = svc.get_settings(TRI054S).settings
        assert (settings.advanced["hdr.output"], settings.pixel_format) == ("HDR", "BayerRG24")

        light["level"] = 0.02  # dimmed, not yet enough for a full sweep
        second = ses.step_stage(0)
        assert second.status == "fail"
        why = second.data["problems"]["HDRChannel0"]
        assert why.startswith("too bright for a full sweep")
        assert "f-stops: close the iris (f/1.4 → about f/" in why
        factor = float(why.split("about ")[1].split("×")[0])
        assert factor == pytest.approx(10 * 86.264 * 8 / (0.8 * (4095 - black)), rel=0.15)

        light["level"] = 0.005  # iris closed further
        ses.set_iris(8.0)
        third = ses.step_stage(0)
        assert third.status == "pass", third.message
        assert third.data["channels"]["HDRChannel0"]["lens"] == "Kowa 8 mm, f/8"
        assert third.data["channels"]["HDRChannel1"]["lens"] == "Kowa 8 mm, f/1.4"
        for out, ch in third.data["channels"].items():
            ptc = ch["photon_transfer"]
            assert ptc["system_gain_dn_per_e"] == pytest.approx(k_dn_e[out], rel=0.1), out
            assert ptc["full_well_e"] == pytest.approx((4095 - black) / k_dn_e[out], rel=0.12)
            assert ch["snr"]
    finally:
        svc.shutdown()


def test_iris_hint() -> None:
    from camera_lab.two_d.fingerprint.session import iris_hint

    assert (
        iris_hint(8.0, "Kowa 8 mm, f/1.4") == " ≈ 3.0 f-stops: close the iris (f/1.4 → about f/4)"
    )
    assert iris_hint(0.5, "F/4") == " ≈ 1.0 f-stops: open the iris (f/4 → about f/2.8)"
    assert iris_hint(4.0, "") == " ≈ 2.0 f-stops: close the iris"


def test_response_is_linear_without_photon_transfer() -> None:
    pts = [
        fm.PtcPoint(exposure_us=e, mean_dn=min(100 + 10 * e, 16_777_215), var_dn2=10 * e + 4)
        for e in (10, 100, 1_000, 10_000, 100_000, 1_000_000, 2_000_000)
    ]
    r = fm.response(pts, 100)
    assert r.saturation_dn == 16_777_215
    assert r.linearity_error_pct < 0.01
    sig, snr = r.snr[-1]
    assert sig == pytest.approx(10_000_000) and snr == pytest.approx(10_000_000 / np.sqrt(1e7 + 4))
    assert all(s < 0.97 * 16_777_215 for s, _ in r.snr)


def test_hot_pixel_count_is_not_the_list_cap() -> None:
    dark = np.full((100, 100), 100.0)
    dark.flat[: fm.MAX_HOT_PIXELS + 500] = 500.0
    hot, total = fm.hot_pixels(dark)
    assert len(hot) == fm.MAX_HOT_PIXELS and total == fm.MAX_HOT_PIXELS + 500


def test_photon_transfer_read_noise_comes_from_the_darks() -> None:
    k = 0.9
    pts = [  # shot noise only: the fit's intercept is 0
        fm.PtcPoint(exposure_us=e, mean_dn=100 + 40 * e, var_dn2=k * 40 * e)
        for e in range(1, 100, 5)
    ]
    assert fm.photon_transfer(pts, 100).read_noise_e == pytest.approx(0, abs=1e-3)
    assert fm.photon_transfer(pts, 100, 1.8).read_noise_e == pytest.approx(2.0)


def test_channel_too_dark_says_how_much_more_light(
    tmp_roots: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc = _tri054s(tmp_roots)
    try:
        svc.open(TRI054S, "test")
        ses = FingerprintSession(svc, TRI054S, root=tmp_roots / "fp")
        _discovery(ses)
        assert ses.step_identity().ok
        ses.results["flat"] = FpResult(step="flat", status="pass", message="")
        ses.hdr_channels = ("HDRChannel3",)
        ses._channel_black["HDRChannel3"] = fm.DarkPoint(
            gain_db=0, exposure_us=1e4, black_dn=200.0, read_noise_dn=2.0, dsnu_dn=0.5
        )
        e_hi = min(ses._range("exposure_us")[1], 2e6)
        level = 0.2 * (4095 - 200) / e_hi  # DN/µs: 20 % of the span at the longest exposure

        def plane() -> np.ndarray:
            s = svc.get_settings(TRI054S).settings
            return np.full((50, 50), 200.0 + level * s.exposure_us)

        monkeypatch.setattr(ses, "_plane", plane)
        res = ses.step_stage(0)
        assert res.status == "fail"
        why = res.data["problems"]["HDRChannel3"]
        assert why.startswith("too dark at the longest exposure")
        assert float(why.split("about ")[1].split("×")[0]) == pytest.approx(4.0, rel=0.05)
    finally:
        svc.shutdown()


def test_hdr_recipe_stages_verify_the_light(
    tmp_roots: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The TRI054S recipe (floods full; f/1.4, f/8, f/16): the light is measured before each
    stage; a wrong iris stops the stage; the light must come back to the reference."""
    from camera_lab.two_d.fingerprint.bench import load_bench

    svc = _tri054s(tmp_roots)
    try:
        svc.open(TRI054S, "test")
        ses = FingerprintSession(svc, TRI054S, root=tmp_roots / "fp")
        assert ses.recipe is not None and ses.lens == "Kowa 8 mm, f/1.4"
        assert ses.min_exposure_us == 2000.0  # from the bench file
        ses.min_exposure_us = 0.0  # stage logic under test, not the bench's exposure floor
        labels = [
            ses.plan[s].label for s in ("cap", "diffuser", "light_2", "light_3", "lens", "finish")
        ]
        assert labels == [
            "1. Lens cap on",
            "2. Diffuser on, iris f/1.4",
            "3. Iris f/8 (HDR ch 2)",
            "4. Iris f/16 (HDR ch 1)",
            "5. Lens / iris",
            "6. Iris back: finish",
        ]
        assert ses.plan["lens"].checks == ("Iris at f/2.8", "Diffuser still on the lens")
        assert ses.plan["finish"].checks[-1] == (
            "OK to write test values to UserSet2 and restart the camera"
        )
        assert ses.plan["diffuser"].measures == (
            "timing",
            "flat",
            "colour",
            "channels_a",
            "noise_gain",
        )
        assert "HDR ch 3" in ses.plan["diffuser"].instruction
        assert not ses.plan["light_4"].active
        assert ses.plan["light_2"].checks == (
            "Bench light on dimmer 10",
            "Iris at f/8",
            "Diffuser still on the lens",
        )
        assert "Iris at f/1.4 (fully open)" in ses.plan["cap"].checks
        with pytest.raises(InvalidRequestError, match="not in the catalog"):
            ses.select_lens("nope")  # lenses are picked from the catalog, never typed
        assert ses.lens == "Kowa 8 mm, f/1.4"
        assert ses.step_identity().ok

        bench = load_bench()
        assert bench is not None
        state = {"dimmer": 10, "f": 1.4}

        kowa = bench.lenses["kowa_8mm"]

        def light() -> float:  # relative to dimmer 10, f/1.4 (the lens as measured)
            return bench.light.relative[int(state["dimmer"])] * kowa.relative(state["f"])

        black, comb_black = 205.0, 1000.0
        sens = {
            "HDRChannel0": 9000.0,
            "HDRChannel1": 300.0,
            "HDRChannel2": 50.0,
            "HDRChannel3": 2.0,
        }
        comb = 20_000.0  # combined output DN/µs at light 1
        rng = np.random.default_rng(2)

        def plane() -> np.ndarray:
            st = svc.get_settings(TRI054S).settings
            out = str(st.advanced["hdr.output"])
            if out == "HDR":
                level = comb_black + comb * light() * st.exposure_us
                return np.full((50, 50), min(level, (1 << 24) - 1))
            sig = sens[out] * light() * st.exposure_us
            img = black + sig + rng.normal(0, np.sqrt(0.5 * sig + 4.0), (200, 200))
            return np.clip(img, 0, 4095)

        monkeypatch.setattr(ses, "_plane", plane)
        ses.results["flat"] = FpResult(step="flat", status="pass", message="")
        ses.black_dn = comb_black
        ses._ref_rate = comb * light()  # what the flat step measured at the reference
        ses._ref_exposure = 50_000.0
        for out in ses.hdr_channels:
            ses._channel_black[out] = fm.DarkPoint(
                gain_db=0, exposure_us=1e4, black_dn=black, read_noise_dn=2.0, dsnu_dn=0.5
            )
        with pytest.raises(InvalidRequestError, match="confirm the checklist"):
            ses.run("light_2", ["Bench light on dimmer 10"])

        a = ses.step_stage(0)
        assert a.status == "pass", a.message
        assert set(a.data["channels"]) == {"HDRChannel3"}
        assert a.data["light"]["measured_vs_expected"] == pytest.approx(1.0, rel=0.02)

        b = ses.step_stage(1)  # the human left the iris at f/1.4
        assert b.status == "fail"
        assert b.message.startswith("the light is 25.91× what dimmer 10, f/8 should give")
        assert "HDRChannel2" not in ses._channels  # nothing measured at the wrong light
        state["f"] = 8
        b = ses.run("light_2", ses.plan["light_2"].checks)
        assert b.status == "pass", b.message
        ch2 = b.data["channels_b"]["channels"]["HDRChannel2"]
        assert ch2["lens"] == "Kowa 8 mm, f/8"
        assert len(ch2["repeats"]["system_gain_dn_per_e"]["values"]) == 3
        assert "3 runs agree within" in b.message

        state["f"] = 16
        c = ses.run("light_3", ses.plan["light_3"].checks)
        assert c.status == "pass", c.message
        assert ses.step_stage(3).status == "skipped"

        back = ses.step_light_back()  # iris still at f/16
        assert back.status == "fail" and "check the dimmer and the iris" in back.message
        state["f"] = 1.4
        back = ses.step_light_back()
        assert back.status == "pass", back.message
        assert ses.lens == "Kowa 8 mm, f/1.4"
    finally:
        svc.shutdown()


def test_recipe_stage_off_tells_the_human_not_to_improvise(
    tmp_roots: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc = _tri054s(tmp_roots)
    try:
        svc.open(TRI054S, "test")
        ses = FingerprintSession(svc, TRI054S, root=tmp_roots / "fp")
        assert ses.step_identity().ok
        ses.results["flat"] = FpResult(step="flat", status="pass", message="")
        ses.black_dn, ses._ref_rate, ses._ref_exposure = 0.0, 1000.0, 10_000.0
        ses._channel_black = {
            o: fm.DarkPoint(
                gain_db=0, exposure_us=1e4, black_dn=205.0, read_noise_dn=2.0, dsnu_dn=0.5
            )
            for o in ses.hdr_channels
        }

        def plane() -> np.ndarray:  # the light is right; the channels are far too bright
            st = svc.get_settings(TRI054S).settings
            if st.advanced["hdr.output"] == "HDR":
                return np.full((20, 20), 1000.0 * st.exposure_us)
            return np.full((20, 20), 4095.0)

        monkeypatch.setattr(ses, "_plane", plane)
        res = ses.step_stage(0)
        assert res.status == "fail"
        assert "the RECIPE is off for this camera" in res.message
        assert "do not change the light yourself" in res.message
    finally:
        svc.shutdown()


def test_bench_light_ratio() -> None:
    from camera_lab.two_d.fingerprint.bench import Bench, BenchLens, BenchLight, load_bench

    lens = BenchLens(label="test", f_stops=(1.4, 2.8, 8), transmission={2.8: 0.36})
    bench = Bench(
        schema_version=1,
        light=BenchLight(label="t", relative={1: 0.2, 10: 1.0}),
        lenses={"t": lens},
    )
    assert bench.light_ratio(1, 2.8, 1, 1.4) == pytest.approx(0.25)  # nominal
    assert bench.light_ratio(1, 2.8, 1, 1.4, lens) == pytest.approx(0.36)  # measured
    assert lens.relative(8) == pytest.approx(0.36 * (2.8 / 8) ** 2)  # from f/2.8
    assert bench.light_ratio(10, 1.4, 1, 1.4) == pytest.approx(5.0)
    with pytest.raises(ValueError, match="no setting 11"):
        bench.light_ratio(11, 1.4, 1, 1.4)

    real = load_bench()  # the bench file: floods, measured Kowa transmission
    assert real is not None and real.min_exposure_us == 2000
    kowa = real.lenses["kowa_8mm"]
    assert kowa.open_f == 1.4 and kowa.relative(16) == pytest.approx(0.0094)


def test_setups_run_each_bench_state_once(svc: TwoDService, tmp_path: Path) -> None:
    """One Run per bench state: cap (identity + darks ×3), diffuser (timing, flat ×3,
    noise), finish (user sets, report)."""
    ses = FingerprintSession(svc, SIM, root=tmp_path / "fp", reset=False)
    assert [s for s in ses.plan if ses.plan[s].active] == [
        "cap",
        "diffuser",
        "light_2",
        "lens",
        "finish",
    ]
    assert ses.plan["cap"].label == "1. Lens cap on"
    assert ses.plan["cap"].measures == ("identity", "dark")
    assert ses.plan["diffuser"].measures == ("timing", "flat", "colour", "noise_gain")
    assert ses.plan["light_2"].label == "3. HDR channels (discovery)"  # sim: HDR-capable
    assert ses.next_step() == "cap"
    with pytest.raises(InvalidRequestError, match="confirm the checklist"):
        ses.run("cap", ["Lens cap on"])
    assert ses.lens_id is None and "Lens picked in the list" in ses.plan["cap"].checks
    with pytest.raises(InvalidRequestError, match="pick the lens"):  # never "not stated"
        ses.run("cap", ses.plan["cap"].checks)
    assert {lid for lid, _, _ in ses.lens_choices} == {"jai_6mm_0628", "kowa_8mm"}
    ses.select_lens("kowa_8mm")  # the bench file's list: the temp workspace catalog is empty
    assert ses.lens == "Kowa 8 mm, f/1.4" and "Iris at f/1.4 (fully open)" in ses.plan["cap"].checks

    stop = ses.run("cap", ses.plan["cap"].checks)  # the chart is in view: no cap
    assert stop.status == "fail" and stop.message.startswith("darks: light reaches the sensor")
    assert ses.next_step() == "cap"
    _scene(svc, "cap")
    cap = ses.run("cap", ses.plan["cap"].checks)
    assert cap.status == "pass", cap.message
    assert set(cap.data) == {"identity", "dark", "durations_s", "temperature_c"}
    assert set(cap.data["durations_s"]) == {"identity", "dark"}
    assert ses.durations["cap"] == cap.data["durations_s"]
    reps = cap.data["dark"]["repeats"]
    assert len(reps["black_dn"]["values"]) == 3 and reps["black_dn"]["spread_pct"] < 1
    assert "3 runs agree within" in cap.message

    _scene(svc, "flat")
    dif = ses.run("diffuser", ses.plan["diffuser"].checks)
    assert dif.status == "pass", dif.message
    assert len(dif.data["flat"]["repeats"]["flat.prnu_pct"]["values"]) == 3
    pipe = dif.data["timing"]["pipeline"]
    assert pipe["frame_interval_ms"] > 0 and pipe["process_ms"] > 0 and "CPUs" in pipe["pc"]
    assert set(dif.data["durations_s"]) == {"timing", "flat", "colour", "noise_gain"}
    with pytest.raises(InvalidRequestError, match="cannot be skipped"):
        ses.skip("diffuser", "no")
    assert ses.run("light_2", ses.plan["light_2"].checks).status == "skipped"  # no channels
    cam = svc._open[SIM].camera
    with pytest.raises(InvalidRequestError, match="fixed once a row has run"):
        ses.select_lens("jai_6mm_0628")
    cam.iris_transmission = 0.25  # type: ignore[attr-defined]  # the human closes 2 stops
    assert ses.plan["lens"].checks[0] == "Iris at f/2.8"
    lens = ses.run("lens", ses.plan["lens"].checks)
    assert lens.status == "pass", lens.message
    cam.iris_transmission = 1.0  # type: ignore[attr-defined]
    fin = ses.run("finish", ses.plan["finish"].checks)
    assert fin.status == "pass", fin.message
    assert ses.next_step() is None
    doc = yaml.safe_load((Path(fin.data["report"]["folder"]) / "fingerprint.yaml").read_text())
    md = (Path(fin.data["report"]["folder"]) / "report.md").read_text()
    assert "| cap | identity" in md and "**all**" in md and "Image pipeline (" in md
    assert "| finish | light_back" in md and "user_sets" in md.split("| finish |")[1]
    assert "Camera temperature:** cap 41.5 °C" in md
    assert "**lens_iris** (pass): Kowa 8 mm, f/2.8: light 0.2" in md
    assert doc["setups"]["finish"]["status"] == "pass"  # the row that wrote the report
    assert doc["lens_iris"]["lens"] == "Kowa 8 mm, f/2.8"
    assert doc["identity"]["lens"] == "Kowa 8 mm, f/1.4"
    assert doc["setups"]["cap"]["status"] == "pass"
    assert doc["dark"]["repeats"]["read_noise_dn"]["median"] > 0


def test_operator_can_skip_a_light_setup(tmp_roots: Path) -> None:
    svc = _tri054s(tmp_roots)
    try:
        svc.open(TRI054S, "test")
        ses = FingerprintSession(svc, TRI054S, root=tmp_roots / "fp")
        res = ses.skip("light_3", "  lens lets in more light than the recipe  ")
        assert res.status == "skipped"
        assert res.message == "skipped by the operator (lens lets in more light than the recipe)"
        assert ses.results["channels_c"].status == "skipped"
    finally:
        svc.shutdown()


def test_flicker_stops_a_channel_sweep(tmp_roots: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A light rippling faster than the exposure: row bands that change every frame."""
    svc = _tri054s(tmp_roots)
    try:
        svc.open(TRI054S, "test")
        ses = FingerprintSession(svc, TRI054S, root=tmp_roots / "fp")
        _discovery(ses)
        assert ses.step_identity().ok
        ses.results["flat"] = FpResult(step="flat", status="pass", message="")
        ses.hdr_channels = ("HDRChannel3",)
        ses._channel_black["HDRChannel3"] = fm.DarkPoint(
            gain_db=0, exposure_us=1e4, black_dn=200.0, read_noise_dn=2.0, dsnu_dn=0.5
        )
        ses._row_noise_dn2["HDRChannel3"] = 0.0
        rng = np.random.default_rng(5)
        rows = np.arange(200)[:, None]

        def plane() -> np.ndarray:
            st = svc.get_settings(TRI054S).settings
            sig = 0.12 * st.exposure_us
            ripple = 1 + 0.05 * np.sin(2 * np.pi * rows / 5 + rng.uniform(0, 2 * np.pi))
            return 200.0 + sig * ripple + rng.normal(0, np.sqrt(0.5 * sig + 4), (200, 200))

        monkeypatch.setattr(ses, "_plane", plane)
        res = ses.step_stage(0)
        assert res.status == "fail"
        why = res.data["problems"]["HDRChannel3"]
        assert why.startswith("the light flickers: row banding adds")
        assert "less light (smaller iris) so the exposures can be longer" in why
    finally:
        svc.shutdown()


def test_lit_measurements_respect_the_bench_minimum(svc: TwoDService, tmp_path: Path) -> None:
    ses = FingerprintSession(svc, SIM, root=tmp_path / "fp", reset=False)
    assert svc.get_profile(SIM).characteristics.shutter == "global"  # the bench's global min
    assert ses.min_exposure_us == 1000.0  # (rolling shutter: 2000, see the TRI054S tests)
    assert ses._lit_range()[0] >= 1000.0


def test_user_sets_without_a_boot_set_feature(svc: TwoDService, tmp_path: Path) -> None:
    """Like the JAI: no UserSetDefault, it boots from User Set 1 once that is saved. The test
    uses User Set 1, then refills it with factory Default (boots as from Default)."""
    cam = svc._open[SIM].camera
    cam._nvram["boot_set_feature"] = False  # type: ignore[attr-defined]
    ses = FingerprintSession(svc, SIM, root=tmp_path / "fp", reset=False)
    assert ses.plan["finish"].checks[-1] == (
        "OK to write test values to UserSet1 and restart the camera; UserSet1 is then refilled "
        "with factory Default"
    )
    assert ses.step_identity().ok
    res = ses.step_user_sets()
    assert res.ok, res.message
    assert "from UserSet1: kept exposure_us, gain_db" in res.message
    assert "UserSet1 refilled with factory Default" in res.message
    nv = svc._open[SIM].camera._nvram  # type: ignore[attr-defined]
    assert nv["boot_set"] == "UserSet1"
    assert nv["user_sets"]["UserSet1"].exposure_us == svc._open[SIM].camera._factory.exposure_us  # type: ignore[attr-defined]


def test_other_gain_mode_is_measured_on_prism_cameras(
    svc: TwoDService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """JAI: master 0 dB ≈ individual +6 dB. The other mode is measured at its 0 dB."""
    from types import SimpleNamespace

    ses = FingerprintSession(svc, SIM, root=tmp_path / "fp", reset=False)
    state = {"mode": "master", "gain": 0.0, "exp": 1000.0}
    ses.black_dn, ses.full_scale = 100.0, 4095.0

    def settings() -> SimpleNamespace:
        return SimpleNamespace(advanced={"prism.gain_mode": state["mode"]}, gain_db=state["gain"])

    def set_(**kw: object) -> None:
        if "advanced" in kw:
            state["mode"] = kw["advanced"]["prism.gain_mode"]  # type: ignore[index]
        if "exposure_us" in kw:
            state["exp"] = float(kw["exposure_us"])  # type: ignore[arg-type]

    def gain_row(g: float, exp: float) -> dict[str, object]:
        state["exp"] = exp
        k = 0.9 if state["mode"] == "master" else 0.45  # individual: half the DN per e-
        sig = (2.0 if state["mode"] == "master" else 1.0) * exp
        return {
            "gain_db": g,
            "exposure_us": exp,
            "signal_dn": sig,
            "gain_dn_per_e": k,
            "full_well_e": round(3995 / k),
        }

    def plane() -> np.ndarray:  # individual mode: the sensor clips at 3 000 DN above black
        rate = 2.0 if state["mode"] == "master" else 1.0
        return np.full((20, 20), 100.0 + min(rate * state["exp"], 3000.0))

    monkeypatch.setattr(ses, "_settings", settings)
    monkeypatch.setattr(ses, "_set", set_)
    monkeypatch.setattr(ses, "_gain_row", gain_row)
    monkeypatch.setattr(ses, "_plane", plane)
    monkeypatch.setattr(ses, "_range", lambda name: (0.0, 18.0))
    out = ses._other_gain_mode(500.0, 10.0, 1e6)
    assert out["signal_ratio"] == pytest.approx(0.5)
    assert out["individual"]["clips_at_dn"] == pytest.approx(3100.0)
    assert out["individual"]["full_well_e"] == round(3000 / 0.45)
    assert state["mode"] == "master"  # put back


def test_jai_recipe_uses_the_end_stop_and_the_global_shutter_minimum() -> None:
    """Derek 2026-10-24: every row the human returns to is an end stop (fully open); the
    JAI's global shutter allows the bench's 1 ms minimum, which makes f/2.8 usable."""
    from camera_lab.two_d.profiles import load_profile

    class _Cam:
        info = type("Info", (), {"model": "AP-3200T-PGE"})()

    class _Svc:
        _open: ClassVar[dict[str, object]] = {"U330439": type("Open", (), {"camera": _Cam()})()}

        def resolve(self, c: str) -> str:
            return c

        def get_profile(self, c: str) -> object:
            return load_profile("U330439")

        def user_set_info(self, c: str) -> dict[str, object]:
            return {"slots": ["Default", "UserSet1"], "boot_set_feature": False}

    ses = FingerprintSession(_Svc(), "U330439")  # type: ignore[arg-type]
    assert ses.lens == "JAI 6 mm 0628-C3, f/2.8" and ses.min_exposure_us == 1000.0
    assert "Iris at f/2.8 (fully open)" in ses.plan["diffuser"].checks
    assert "Iris at f/2.8 (fully open)" in ses.plan["finish"].checks
    assert ses.plan["lens"].checks[0] == "Iris at f/5.6"
