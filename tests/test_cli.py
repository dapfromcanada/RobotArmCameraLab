"""CLI tests: every command, JSON output on stdout, and the exit-code table."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from camera_lab import cli_support
from camera_lab.cli import app
from camera_lab.core.locks import CameraLock
from camera_lab.two_d.drivers.simulated import SimulatedCamera
from camera_lab.two_d.errors import CaptureTimeoutError, SdkUnavailableError
from camera_lab.two_d.service import TwoDService

runner = CliRunner()


def run(*args: str) -> Any:
    return runner.invoke(app, list(args))


def ok_json(*args: str) -> Any:
    res = run(*args, "--json")
    assert res.exit_code == 0, res.output
    data = json.loads(res.stdout)  # stdout must be pure JSON (logs go to stderr)
    assert data["ok"] is True
    return data["result"]


def _capture_id() -> str:
    [cap] = ok_json("--sim", "two-d", "capture", "SIM-0001")
    return str(cap["capture_id"])


# ---------------------------------------------------------------- basics
def test_help_lists_groups() -> None:
    res = run("--help")
    assert res.exit_code == 0
    for group in ("two-d", "light", "limits"):
        assert group in res.stdout
    sub = run("two-d", "--help")
    for cmd in ("discover", "settings", "capture", "score", "tune", "preset", "runs"):
        assert cmd in sub.stdout


def test_no_args_shows_help() -> None:
    assert run().exit_code in (0, 2)


# -------------------------------------------------------------- discover
def test_discover() -> None:
    cams = ok_json("two-d", "discover", "--sim")
    assert [c["serial"] for c in cams] == ["SIM-0001"]
    assert ok_json("--sim", "two-d", "discover")[0]["driver"] == "simulated"
    assert "SIM-0001" in run("--sim", "two-d", "discover").stdout
    assert ok_json("two-d", "discover") == []  # no SDK drivers yet, no --sim
    assert "no cameras found" in run("two-d", "discover").stdout


# -------------------------------------------------------------- settings
def test_settings_get() -> None:
    view = ok_json("--sim", "two-d", "settings", "get", "SIM-0001")
    assert view["ranges"]["gain_db"]["max"] == 24  # camera range (limits policy, schema 2)
    assert view["working_range"]["gain_db"]["max"] == 18
    res = run("--sim", "two-d", "settings", "get", "sim")  # alias from cameras.yaml
    assert res.exit_code == 0 and "effective ranges" in res.stdout


def test_settings_set_ok_and_rejected() -> None:
    data = ok_json(
        "--sim", "two-d", "settings", "set", "SIM-0001",
        "--exposure-us", "20000", "--gain-db", "3", "--wb", "1.2,1,1.4",
        "--roi", "0,0,512,512", "--fps", "15", "--trigger", "software",
        "--pixel-format", "RGB8", "--gamma", "1.0",
    )  # fmt: skip
    assert all(r["status"] != "rejected" for r in data["results"])
    assert data["settings"]["roi"]["width"] == 512
    res = run("--sim", "two-d", "settings", "set", "SIM-0001", "--gain-db", "30")
    assert res.exit_code == 4
    assert "gain_db 30.0 > camera max 24.0" in res.stdout


@pytest.mark.parametrize(
    "extra",
    [
        [],
        ["--roi", "1,2,3"],
        ["--roi", "a,b,c,d"],
        ["--wb", "1,2"],
        ["--wb", "x,1,1"],
        ["--trigger", "hardware"],
        ["--exposure-us", "abc"],
    ],
)
def test_settings_set_usage_errors(extra: list[str]) -> None:
    assert run("--sim", "two-d", "settings", "set", "SIM-0001", *extra).exit_code == 2


def test_settings_set_validation_error_json() -> None:
    res = run("--sim", "two-d", "settings", "set", "SIM-0001", "--roi", "-1,0,16,16", "--json")
    assert res.exit_code == 2
    assert json.loads(res.stdout)["error"]["code"] == "invalid_argument"


# ---------------------------------------------------------------- capture
def test_capture_and_runs(tmp_roots: Path) -> None:
    caps = ok_json("--sim", "two-d", "capture", "SIM-0001", "--count", "2", "--note", "cli test")
    assert len(caps) == 2 and caps[0]["run_id"].endswith("_cli-test")
    human = run("--sim", "two-d", "capture", "SIM-0001")
    assert human.exit_code == 0 and "composite" in human.stdout
    runs = ok_json("two-d", "runs", "list")
    assert len(runs) == 2
    assert (
        ok_json("two-d", "runs", "list", "--camera", "SIM-0001")[0]["camera_serial"] == "SIM-0001"
    )
    assert "manual" in run("two-d", "runs", "list").stdout
    shown = ok_json("two-d", "runs", "show", caps[0]["run_id"])
    assert shown["method"] == "manual"
    assert run("two-d", "runs", "show", caps[0]["run_id"]).exit_code == 0
    assert run("two-d", "runs", "show", "20261021-101500_SIM-0001_nope").exit_code == 2


def test_runs_list_empty() -> None:
    assert "no runs" in run("two-d", "runs", "list").stdout


def test_capture_errors() -> None:
    assert run("two-d", "capture", "SIM-0001").exit_code == 3  # no --sim: not found
    assert run("--sim", "two-d", "capture", "SIM-0001", "--run", "bogus").exit_code == 2
    assert run("--sim", "two-d", "capture", "SIM-0001", "--count", "0").exit_code == 2


def test_camera_busy_exit_3() -> None:
    with CameraLock("SIM-0001", owner="gui"):
        res = run("--sim", "two-d", "capture", "SIM-0001", "--json")
    assert res.exit_code == 3
    err = json.loads(res.stdout)["error"]
    assert err["code"] == "camera_busy" and "gui" in err["message"]


# ------------------------------------------------------------------ score
def test_score(tmp_roots: Path) -> None:
    cid = _capture_id()
    run_id, n = cid.rsplit("/", 1)
    tiff = (
        tmp_roots
        / "data"
        / "raw_recordings"
        / "camera_lab"
        / "two_d"
        / run_id
        / "captures"
        / f"{n}.tiff"
    )
    a = ok_json("two-d", "score", str(tiff))
    b = ok_json("two-d", "score", str(tiff))
    assert a == b and "composite" in a
    assert "composite" in run("two-d", "score", str(tiff)).stdout
    assert run("two-d", "score", str(tiff.with_suffix(".png"))).exit_code == 2


# ------------------------------------------------------------------- tune
def test_tune_classic_writes_complete_run(tmp_roots: Path) -> None:
    res = ok_json("--sim", "two-d", "tune", "SIM-0001", "--method", "classic")
    assert res["converged"] is True and res["steps_used"] <= 15
    run_dir = tmp_roots / "data" / "raw_recordings" / "camera_lab" / "two_d" / res["run_id"]
    assert (run_dir / "run.json").is_file() and (run_dir / "steps.jsonl").is_file()
    assert list((run_dir / "captures").glob("*.tiff")) and list(
        (run_dir / "previews").glob("*.jpg")
    )
    rec = json.loads((run_dir / "run.json").read_text())
    assert rec["method"] == "classic" and rec["best_capture_id"] == res["best_capture_id"]
    human = run("--sim", "two-d", "tune", "SIM-0001", "--target-mean", "0.4", "--wb")
    assert human.exit_code == 0 and "converged" in human.stdout


def test_tune_errors() -> None:
    assert run("--sim", "two-d", "tune", "SIM-0001", "--method", "agent").exit_code == 2
    assert run("--sim", "two-d", "tune", "SIM-0001", "--target-mean", "0.95").exit_code == 2
    assert run("--sim", "two-d", "tune", "SIM-0001", "--max-steps", "41").exit_code == 4


# ---------------------------------------------------------------- presets
def test_preset_commands() -> None:
    cid = _capture_id()
    saved = ok_json("--sim", "two-d", "preset", "save", "SIM-0001", "bench", "--from-capture", cid)
    assert saved["path"].endswith("SIM-0001/bench.yaml")
    assert (
        run(
            "--sim", "two-d", "preset", "save", "SIM-0001", "bench", "--from-capture", cid
        ).exit_code
        == 2
    )
    assert (
        run(
            "--sim",
            "two-d",
            "preset",
            "save",
            "SIM-0001",
            "bench",
            "--from-capture",
            cid,
            "--force",
        ).exit_code
        == 0
    )
    assert [p["name"] for p in ok_json("two-d", "preset", "list")] == ["bench"]
    assert "bench" in run("two-d", "preset", "list", "SIM-0001").stdout
    assert "no presets" in run("two-d", "preset", "list", "U330439").stdout
    assert ok_json("two-d", "preset", "show", "SIM-0001", "bench")["name"] == "bench"
    applied = ok_json("--sim", "two-d", "preset", "apply", "SIM-0001", "bench")
    assert applied["ok"] is True
    assert run("--sim", "two-d", "preset", "apply", "SIM-0001", "bench").exit_code == 0
    assert run("--sim", "two-d", "preset", "apply", "SIM-0001", "nope").exit_code == 2
    assert run("two-d", "preset", "show", "SIM-0001", "nope").exit_code == 2


def test_preset_apply_rejected_exit_4(tmp_roots: Path) -> None:
    cid = _capture_id()
    ok_json("--sim", "two-d", "preset", "save", "SIM-0001", "hot", "--from-capture", cid)
    path = tmp_roots / "config" / "camera_presets" / "two_d" / "SIM-0001" / "hot.yaml"
    path.write_text(path.read_text().replace("gain_db: 0.0", "gain_db: 30.0"))
    assert run("--sim", "two-d", "preset", "apply", "SIM-0001", "hot").exit_code == 4


# ----------------------------------------------------------------- lights
def test_light_commands() -> None:
    chans = ok_json("--sim", "light", "list")
    assert [c["id"] for c in chans] == ["white_ring", "white_bar", "red", "blue"]
    assert "white_ring" in run("light", "list").stdout
    res = ok_json("--sim", "light", "set", "white_ring=40", "red=10")
    assert res["levels"]["white_ring"] == 40 and res["ok"] is True
    assert "applied" in run("light", "set", "red=5").stdout
    over = run("light", "set", "white_ring=95")
    assert over.exit_code == 4 and "lights.default.max_pct" in over.stdout
    assert run("light", "set", "white_ring").exit_code == 2
    assert run("light", "set", "white_ring=abc").exit_code == 2
    assert ok_json("light", "off")["levels"]["red"] == 0
    assert "all lights off" in run("light", "off").stdout


# ----------------------------------------------------------------- limits
def test_limits_show() -> None:
    data = ok_json("limits", "show")
    assert data["limits"]["lights"]["default"]["max_pct"] == 80
    assert data["source"].endswith("safety_limits.yaml")
    assert "max_steps_hard" in run("limits", "show").stdout


# ------------------------------------------------------- remaining codes
def test_exit_5_sdk_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(self: TwoDService, camera_id: str, owner: str = "cli") -> None:
        raise SdkUnavailableError("driver 'arena' unavailable: No module named 'arena_api'")

    monkeypatch.setattr(TwoDService, "open", boom)
    res = run("two-d", "capture", "233100505", "--json")
    assert res.exit_code == 5
    assert json.loads(res.stdout)["error"]["code"] == "sdk_unavailable"


def test_exit_6_capture_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def timeout(self: SimulatedCamera, timeout_ms: int = 2000) -> None:
        raise CaptureTimeoutError("no frame within 2000 ms")

    monkeypatch.setattr(SimulatedCamera, "grab", timeout)
    assert run("--sim", "two-d", "capture", "SIM-0001").exit_code == 6


def test_exit_1_unexpected(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(**kw: Any) -> None:
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(cli_support, "build_service", broken)
    res = run("limits", "show", "--json")
    assert res.exit_code == 1
    assert json.loads(res.stdout)["error"]["code"] == "internal_error"
    assert "Traceback" not in res.stdout


def test_exit_1_config_invalid(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from camera_lab.core import safety

    monkeypatch.setattr(safety, "default_limits_path", lambda: tmp_path / "missing.yaml")
    res = run("limits", "show")
    assert res.exit_code == 1 and "refusing to start" in res.output
    assert run("--sim", "limits", "show").exit_code == 0  # sim falls back to built-in limits


def test_log_level_and_quiet() -> None:
    assert run("--log-level", "bogus", "limits", "show").exit_code == 2
    res = run("-q", "--sim", "two-d", "discover")
    assert res.exit_code == 0
