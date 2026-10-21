"""Tests for two_d.models."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import numpy as np
import pytest
from pydantic import ValidationError

from camera_lab.two_d.models import (
    ROI,
    ApplyResult,
    CameraInfo,
    CameraSettings,
    CameraSettingsPatch,
    FieldResult,
    Frame,
    MetricResult,
    ScoreReport,
    SettingSpec,
    is_bayer,
    is_rgb,
)

SETTINGS = CameraSettings(
    exposure_us=1000, gain_db=0, pixel_format="Mono8", roi=ROI(x=0, y=0, width=8, height=4)
)
NOW = datetime(2026, 10, 21, tzinfo=UTC)


def _frame(**kw: Any) -> Frame:
    args: dict[str, Any] = {
        "data": np.zeros((4, 8), np.uint8),
        "pixel_format": "Mono8",
        "bit_depth": 8,
        "timestamp_utc": NOW,
        "frame_id": 1,
        "settings": SETTINGS,
    }
    args.update(kw)
    return Frame(**args)


def test_frame_fields_and_properties() -> None:
    f = _frame()
    assert set(Frame.model_fields) == {
        "data",
        "pixel_format",
        "bit_depth",
        "timestamp_utc",
        "frame_id",
        "settings",
    }
    assert (f.width, f.height, f.full_scale) == (8, 4, 255)
    assert _frame(data=np.zeros((4, 8), np.uint16), bit_depth=12).full_scale == 4095


@pytest.mark.parametrize(
    "kw",
    [
        {"data": np.zeros((4, 8), np.float32)},
        {"data": np.zeros((4, 8, 4), np.uint8)},
        {"data": np.zeros((0, 8), np.uint8)},
        {"data": [[1, 2]]},
        {"timestamp_utc": datetime(2026, 1, 1)},
        {"bit_depth": 0},
        {"frame_id": -1},
    ],
)
def test_frame_validation(kw: dict[str, Any]) -> None:
    with pytest.raises((ValidationError, TypeError, ValueError)):
        _frame(**kw)


def test_frame_is_frozen() -> None:
    f = _frame()
    with pytest.raises(ValidationError):
        f.frame_id = 2


def test_camera_info() -> None:
    info = CameraInfo(
        vendor="JAI Corporation", model="AP-3200T-PGE", serial="U330439", driver="ebus"
    )
    assert info.camera_id == "U330439"
    with pytest.raises(ValidationError):
        CameraInfo(vendor="x", model="y", serial="../x", driver="ebus")


def test_patch_changed_fields() -> None:
    p = CameraSettingsPatch(gain_db=3, roi=ROI(x=0, y=0, width=16, height=16))
    assert p.changed_fields() == ["gain_db", "roi"]
    assert CameraSettingsPatch().changed_fields() == []
    with pytest.raises(ValidationError):
        CameraSettingsPatch(white_balance={"purple": 1.0})  # type: ignore[dict-item]
    with pytest.raises(ValidationError):
        CameraSettingsPatch(trigger_mode="hardware")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        CameraSettingsPatch(exposure_us=float("nan"))


def test_roi_validation() -> None:
    with pytest.raises(ValidationError):
        ROI(x=-1, y=0, width=1, height=1)
    with pytest.raises(ValidationError):
        ROI(x=0, y=0, width=0, height=1)


def test_setting_spec() -> None:
    s = SettingSpec(name="gain_db", kind="float", min=0, max=24, increment=0.1)
    r = s.camera_range()
    assert (r.min, r.max, r.increment) == (0, 24, 0.1)
    with pytest.raises(ValidationError):
        SettingSpec(name="pf", kind="enum")
    with pytest.raises(ValidationError):
        SettingSpec(name="g", kind="float", min=5, max=1)
    with pytest.raises(ValueError, match="not numeric"):
        SettingSpec(name="pf", kind="enum", options=("Mono8",)).camera_range()


def test_apply_result_ok() -> None:
    ok = ApplyResult(results=(FieldResult(field="gain_db", status="applied"),), settings=SETTINGS)
    assert ok.ok
    bad = ApplyResult(
        results=(FieldResult(field="gain_db", status="rejected", reason="x"),), settings=SETTINGS
    )
    assert not bad.ok


def test_score_report_json_is_canonical() -> None:
    r = ScoreReport(
        metrics=(MetricResult(name="mean_level", value=0.5, passed=True),),
        histogram=(1.0,),
        sub_scores={"noise": 1.0, "exposure": 0.8},
        composite=80.0,
        params={"target_mean": 0.45},
    )
    assert r.metric("mean_level") == 0.5
    with pytest.raises(KeyError):
        r.metric("nope")
    raw = r.to_json()
    assert b" " not in raw
    assert json.loads(raw)["sub_scores"] == {"exposure": 0.8, "noise": 1.0}
    assert list(json.loads(raw)) == sorted(json.loads(raw))


def test_format_helpers() -> None:
    assert is_bayer("BayerRG8") and not is_bayer("RGB8")
    assert is_rgb("RGB8") and not is_rgb("Mono8")
