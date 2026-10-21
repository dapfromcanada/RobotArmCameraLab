"""Tests for the read-only calibration guard."""

from __future__ import annotations

from pathlib import Path

import pytest

from camera_lab.core.paths import UnsafePathError
from camera_lab.two_d.calib_guard import calibration_files, calibration_warnings


@pytest.fixture
def calib(tmp_path: Path) -> Path:
    root = tmp_path / "calibration"
    (root / "intrinsics").mkdir(parents=True)
    (root / "extrinsics").mkdir()
    (root / "intrinsics" / "U330439_2026-10-01.yaml").write_text("x")
    (root / "extrinsics" / "U330439").mkdir()
    (root / "intrinsics" / ".U330439.swp").write_text("x")
    (root / "intrinsics" / "233100505.yaml").write_text("x")
    return root


def test_warns_on_roi_change(calib: Path) -> None:
    w = calibration_warnings("U330439", ["roi"], calib)
    assert w == [
        f"this change may invalidate calibration in {calib / 'extrinsics' / 'U330439'}",
        "this change may invalidate calibration in "
        f"{calib / 'intrinsics' / 'U330439_2026-10-01.yaml'}",
    ]


def test_no_warning_for_other_settings(calib: Path) -> None:
    assert calibration_warnings("U330439", ["exposure_us", "gain_db"], calib) == []


def test_no_warning_without_files(calib: Path) -> None:
    assert calibration_warnings("SIM-0001", ["roi"], calib) == []
    assert calibration_warnings("U330439", ["roi"], calib / "missing") == []


def test_read_only_tree_and_no_writes(calib: Path) -> None:
    before = {p: p.stat().st_mtime_ns for p in calib.rglob("*")}
    for d in [calib, *[p for p in calib.rglob("*") if p.is_dir()]]:
        d.chmod(0o555)
    try:
        assert len(calibration_files("U330439", calib)) == 2
        assert calibration_warnings("U330439", ["roi", "focus"], calib)
    finally:
        for d in [calib, *[p for p in calib.rglob("*") if p.is_dir()]]:
            d.chmod(0o755)
    assert {p: p.stat().st_mtime_ns for p in calib.rglob("*")} == before


def test_default_root_uses_ws(tmp_roots: Path) -> None:
    (tmp_roots / "calibration" / "intrinsics").mkdir(parents=True)
    (tmp_roots / "calibration" / "intrinsics" / "SIM-0001.yaml").write_text("x")
    assert calibration_warnings("SIM-0001", ["roi"]) != []


def test_invalid_serial() -> None:
    with pytest.raises(UnsafePathError):
        calibration_files("../x")
