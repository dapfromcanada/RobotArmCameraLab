"""Phase 10 step 2: find library charts in images and record them with captures."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import pytest

from camera_lab.two_d.charts import builtin_charts, init_builtin, tag_owner
from camera_lab.two_d.charts.detect import (
    DETECT_LONG_EDGE,
    chart_to_image,
    charts_from_tags,
    detect_tags,
)
from camera_lab.two_d.charts.draw import rasterize
from camera_lab.two_d.charts.models import ChartSpec, PatchFeature, TagSpec
from camera_lab.two_d.models import Frame
from camera_lab.two_d.service import TwoDService

PX = 4.0  # px per mm of the synthetic renders
SIM = "SIM-0001"


@pytest.fixture
def library(tmp_path: Path) -> dict[int, ChartSpec]:
    init_builtin(tmp_path / "charts")
    return tag_owner(tmp_path / "charts")


def _chart(chart_id: str) -> ChartSpec:
    return next(c for c in builtin_charts() if c.id == chart_id)


def _patch_corners(chart: ChartSpec, name: str) -> np.ndarray:
    f = next(f for f in chart.features if f.name == name)
    assert isinstance(f, PatchFeature)
    return np.array(
        [
            (f.x_mm, f.y_mm),
            (f.x_mm + f.w_mm, f.y_mm),
            (f.x_mm + f.w_mm, f.y_mm + f.h_mm),
            (f.x_mm, f.y_mm + f.h_mm),
        ]
    )


@pytest.mark.parametrize("chart", builtin_charts(), ids=lambda c: c.id)
def test_flat_render_gives_the_chart_and_its_scale(
    chart: ChartSpec, library: dict[int, ChartSpec]
) -> None:
    scan = charts_from_tags(detect_tags(rasterize(chart, PX)), library)
    assert [c.chart_id for c in scan.charts] == [chart.id]
    det = scan.charts[0]
    assert det.version == chart.version and det.type == chart.type
    assert det.px_per_mm == pytest.approx(PX, rel=0.01)
    hom = np.array(det.homography).reshape(3, 3)
    assert hom == pytest.approx(np.diag([PX, PX, 1.0]), abs=0.05 * PX)
    assert scan.unknown_tags == ()


def test_homography_follows_a_tilted_perspective_view(library: dict[int, ChartSpec]) -> None:
    chart = _chart("step-wedge-letter-v1")
    page = rasterize(chart, PX)
    h, w = page.shape
    # a camera looking at the page from an angle: rotation + perspective, on a grey table
    src = np.float32([[0, 0], [w, 0], [w, h], [0, h]])
    dst = np.float32([[300, 180], [w + 160, 260], [w + 60, h + 420], [220, h + 300]])
    warp = cv2.getPerspectiveTransform(src, dst)
    img = cv2.warpPerspective(page, warp, (w + 600, h + 700), borderValue=110)
    scan = charts_from_tags(detect_tags(img), library)
    assert [c.chart_id for c in scan.charts] == [chart.id]
    det = scan.charts[0]
    # patch corners far from the tag land where the true warp puts them
    mm = _patch_corners(chart, "step-20")
    truth = cv2.perspectiveTransform((mm * PX).reshape(-1, 1, 2).astype(np.float64), warp).reshape(
        -1, 2
    )
    assert np.abs(chart_to_image(det, mm) - truth).max() < 4.0  # px, ~1 mm here


def test_dark_12bit_mono_frame_is_stretched_and_detected(library: dict[int, ChartSpec]) -> None:
    chart = _chart("resolution-letter-v1")
    page = rasterize(chart, PX).astype(np.float64) / 255.0
    rng = np.random.default_rng(0)
    dark = 40 + page * 160 + rng.normal(0, 4, page.shape)  # ~5 % of the 12-bit range
    data = np.clip(dark, 0, 4095).astype(np.uint16)
    scan = charts_from_tags(detect_tags(data, "Mono12p"), library)
    assert [c.chart_id for c in scan.charts] == [chart.id]


def test_bayer_frame_is_demosaiced_and_detected(library: dict[int, ChartSpec]) -> None:
    chart = _chart("grey-patch-letter-v1")
    page = rasterize(chart, PX)
    bayer = page.copy()  # a grey scene seen through an RG/GB mosaic: equal channel gains
    bayer[0::2, 1::2] = (page[0::2, 1::2] * 0.8).astype(np.uint8)
    scan = charts_from_tags(detect_tags(bayer, "BayerRG8"), library)
    assert [c.chart_id for c in scan.charts] == [chart.id]


def test_large_sensor_is_downscaled_and_corners_scaled_back(library: dict[int, ChartSpec]) -> None:
    chart = _chart("step-wedge-letter-v1")
    big_px = 12.0  # 2591 × 3353 px: above DETECT_LONG_EDGE
    page = rasterize(chart, big_px)
    assert max(page.shape) > DETECT_LONG_EDGE
    [hit] = detect_tags(page)
    tag = chart.tags[0]
    assert hit.corners_px[0] == pytest.approx((tag.x_mm * big_px, tag.y_mm * big_px), abs=3.0)
    assert hit.side_px == pytest.approx(tag.size_mm * big_px, rel=0.01)


def test_unknown_and_foreign_tags(library: dict[int, ChartSpec]) -> None:
    stray = ChartSpec(
        id="stray-v1",
        type="tag_card",
        version=1,
        title="stray",
        tags=(
            TagSpec(id=150, x_mm=20, y_mm=20, size_mm=40),
            TagSpec(id=199, x_mm=120, y_mm=20, size_mm=40),
        ),
    )
    img = rasterize(stray, PX)
    # draw a tag outside camera_lab's range (id 7) on top: it must be ignored
    d = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_APRILTAG_36h11)
    foreign = cv2.aruco.generateImageMarker(d, 7, 160)
    img[600:760, 300:460] = foreign
    hits = detect_tags(img)
    assert [h.id for h in hits] == [150, 199]
    scan = charts_from_tags(hits, library)
    assert scan.charts == () and scan.unknown_tags == (150, 199)
    assert [r["chart_id"] for r in scan.to_record()] == [None, None]


def test_two_charts_in_one_image(library: dict[int, ChartSpec]) -> None:
    a = rasterize(_chart("step-wedge-letter-v1"), 2.5)
    b = rasterize(_chart("tag-card-letter-v1"), 2.5)
    img = np.full((a.shape[0] + 40, a.shape[1] * 2 + 60), 255, np.uint8)
    img[20 : 20 + a.shape[0], 20 : 20 + a.shape[1]] = a
    img[20 : 20 + b.shape[0], 40 + a.shape[1] : 40 + a.shape[1] + b.shape[1]] = b
    scan = charts_from_tags(detect_tags(img), library)
    assert [c.chart_id for c in scan.charts] == ["step-wedge-letter-v1", "tag-card-letter-v1"]
    card = scan.charts[1]
    assert [t.id for t in card.tags] == [120, 121, 122, 123]
    rec = card.to_record()
    assert rec["tag_ids"] == [120, 121, 122, 123] and len(rec["homography"]) == 9


def test_capture_records_charts_in_sidecar_and_run(
    make_service: Callable[..., TwoDService], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    init_builtin(tmp_path / "charts")
    svc = make_service(charts_root=tmp_path / "charts")
    svc.open(SIM, "test")
    real = svc.grab(SIM)
    page = rasterize(_chart("step-wedge-letter-v1"), 2.0)

    def grab(camera_id: str, timeout_ms: int = 2000) -> Frame:
        return Frame(
            data=page,
            pixel_format="Mono8",
            bit_depth=8,
            timestamp_utc=real.timestamp_utc,
            frame_id=real.frame_id,
            settings=real.settings.model_copy(update={"pixel_format": "Mono8"}),
        )

    monkeypatch.setattr(svc, "grab", grab)
    [res] = svc.capture(SIM, 1, note="chart test")
    assert [c["chart_id"] for c in res.charts] == ["step-wedge-letter-v1"]
    out: dict[str, Any] = res.to_dict()
    assert out["charts"][0]["tag_ids"] == [100] and out["charts"][0]["version"] == 1
    run_dir = res.preview_path.parent.parent if res.preview_path else None
    assert run_dir is not None
    sidecar = json.loads((run_dir / "captures" / "0001.json").read_text())
    assert sidecar["charts"][0]["chart_id"] == "step-wedge-letter-v1"
    assert len(sidecar["charts"][0]["homography"]) == 9
    assert json.loads((run_dir / "run.json").read_text())["charts_seen"] == ["step-wedge-letter-v1"]


def test_capture_without_charts_or_library(
    make_service: Callable[..., TwoDService], tmp_path: Path
) -> None:
    svc = make_service(charts_root=tmp_path / "no-charts")  # missing library: detection off
    svc.open(SIM, "test")
    [res] = svc.capture(SIM, 1)
    assert res.charts == () and res.to_dict()["charts"] == []


def test_invalid_library_does_not_break_capture(
    make_service: Callable[..., TwoDService], tmp_path: Path
) -> None:
    root = tmp_path / "charts"
    root.mkdir()
    (root / "broken-v1.yaml").write_text("id: broken-v1\ntype: nonsense\n")
    svc = make_service(charts_root=root)
    svc.open(SIM, "test")
    [res] = svc.capture(SIM, 1)
    assert res.charts == ()
