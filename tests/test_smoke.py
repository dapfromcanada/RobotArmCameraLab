"""Phase 1 smoke tests: package, CLI skeleton, logging, path roots, pinned env."""

from __future__ import annotations

import importlib
from importlib.metadata import version
from pathlib import Path

import pytest
from typer.testing import CliRunner

import camera_lab
from camera_lab.cli import app
from camera_lab.core import logging as cl_logging
from camera_lab.core import paths

runner = CliRunner()


def test_version() -> None:
    assert camera_lab.__version__ == "0.1.0"
    assert version("camera-lab") == "0.1.0"


def test_cli_version_flag() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == "0.1.0"


@pytest.mark.parametrize("group", ["two-d", "light", "limits"])
def test_cli_subapps_have_help(group: str) -> None:
    result = runner.invoke(app, [group, "--help"])
    assert result.exit_code == 0


def test_cli_bad_log_level_is_usage_error() -> None:
    result = runner.invoke(app, ["--log-level", "bogus", "two-d", "--help"])
    assert result.exit_code == 2


def test_log_level_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CAMERA_LAB_LOG_LEVEL", "debug")
    assert cl_logging.resolve_level() == 10
    assert cl_logging.resolve_level("ERROR") == 40


def test_logging_goes_to_stderr_only(capsys: pytest.CaptureFixture[str]) -> None:
    cl_logging.configure_logging("INFO")
    cl_logging.get_logger("smoke").info("hello", k=1)
    out, err = capsys.readouterr()
    assert out == ""
    assert "hello" in err


def test_path_roots_follow_env(tmp_roots: Path) -> None:
    assert paths.ws_root() == tmp_roots
    assert paths.data_root() == tmp_roots / "data" / "raw_recordings" / "camera_lab"
    assert paths.two_d_runs_root() == paths.data_root() / "two_d"
    assert paths.presets_root() == tmp_roots / "config" / "camera_presets"
    assert paths.two_d_presets_root() == paths.presets_root() / "two_d"
    assert paths.calibration_root() == tmp_roots / "calibration"
    assert (paths.APP_ROOT / "pyproject.toml").is_file()


def test_path_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CAMERA_LAB_WS_ROOT")
    monkeypatch.delenv("CAMERA_LAB_DATA_ROOT")
    assert paths.ws_root() == Path("/mnt/workspaces/robotic_cell")
    assert paths.data_root() == Path("/mnt/workspaces/robotic_cell/data/raw_recordings/camera_lab")


def test_numpy_below_2() -> None:
    import numpy

    assert numpy.__version__ == "1.26.4"


@pytest.mark.parametrize(
    ("dist", "expected"),
    [
        ("opencv-python-headless", "4.11.0.86"),
        ("tifffile", "2026.3.3"),
        ("pydantic", "2.14.1"),
        ("PyYAML", "6.0.3"),
        ("mcp", "2.3.0"),
        ("typer", "0.27.3"),
        ("PySide6-Essentials", "6.12.0"),
        ("structlog", "26.1.0"),
    ],
)
def test_pinned_runtime_versions(dist: str, expected: str) -> None:
    assert version(dist) == expected


def test_mcp_v2_api_present() -> None:
    mod = importlib.import_module("mcp.server.mcpserver")
    assert hasattr(mod, "MCPServer")
    assert hasattr(mod, "Image")


@pytest.mark.hw
@pytest.mark.parametrize("module", ["arena_api", "ebus_base", "ebus_receive"])
def test_vendor_sdk_imports(module: str) -> None:
    pytest.importorskip(module)
