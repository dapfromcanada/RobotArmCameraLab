"""The part area (Phase 13, Derek 2026-10-28): drawn once, saved per camera in the catalog;
scoring, the classic tuner and the site light assessment judge the part."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from typer.testing import CliRunner

from camera_lab.cli import app
from camera_lab.two_d.catalog import Catalog, CatalogError, PartArea, catalog_path
from camera_lab.two_d.catalog.schema import MIGRATIONS
from camera_lab.two_d.errors import InvalidRequestError
from camera_lab.two_d.models import ROI, CameraSettingsPatch
from camera_lab.two_d.part_area import crop_plane, place
from camera_lab.two_d.scoring import (
    FractionalROI,
    ScoringConfig,
    ScoringParamsOverride,
    score_array,
)
from camera_lab.two_d.service import TwoDService

SIM = "SIM-0001"
MODEL = "Simulated2D"


@pytest.fixture
def catalog(tmp_path: Path) -> Path:
    path = tmp_path / "catalog.sqlite"
    with Catalog(path) as c:
        c.upsert_camera_model(MODEL, vendor="camera_lab")
    return path


@pytest.fixture
def svc(make_service: Callable[..., TwoDService], catalog: Path) -> TwoDService:
    s = make_service(catalog=catalog)
    s.open(SIM, "test")
    s.apply(SIM, CameraSettingsPatch(pixel_format="Mono12"))
    return s


def _size(svc: TwoDService) -> tuple[int, int]:
    roi = svc.get_settings(SIM).settings.roi
    return roi.width, roi.height


# ---------------------------------------------------------------- catalog
def test_catalog_stores_one_area_per_camera_and_logs_every_change(catalog: Path) -> None:
    with Catalog(catalog) as c:
        with pytest.raises(CatalogError, match="not in the catalog"):
            c.set_part_area("NOPE", 0, 0, 10, 10)
        c.upsert_camera(SIM, MODEL)
        with pytest.raises(CatalogError, match="invalid"):
            c.set_part_area(SIM, 0, 0, 0, 10)
        assert c.set_part_area(SIM, 10, 20, 30, 40)
        assert not c.set_part_area(SIM, 10, 20, 30, 40)  # unchanged: nothing written
        assert c.set_part_area(SIM, 11, 20, 30, 40)
        area = c.part_area(SIM)
        assert (area.x, area.y, area.width, area.height) == (11, 20, 30, 40)  # type: ignore[union-attr]
        assert c.clear_part_area(SIM)
        assert not c.clear_part_area(SIM)
        assert c.part_area(SIM) is None
        log = [(r["col"], r["old"], r["new"]) for r in c.changes() if r["tbl"] == "part_areas"]
    assert log[0] == (None, "11,20,30,40", "deleted")
    assert ("x", "10", "11") in log
    assert log[-1] == (None, None, "created")


def test_an_old_catalog_is_backed_up_before_it_is_migrated(tmp_path: Path) -> None:
    path = tmp_path / "old.sqlite"
    conn = sqlite3.connect(path)
    for n, script in enumerate(MIGRATIONS[:2]):
        conn.executescript(script + f"UPDATE schema_version SET version = {n + 1};")  # noqa: S608
    conn.close()
    with Catalog(path) as c:
        assert c.version == len(MIGRATIONS)
    [backup] = (tmp_path / "camera_catalog_backups").glob("*.sqlite")
    assert "before-migration-2-to-3" in backup.name
    old = sqlite3.connect(backup)
    assert old.execute("SELECT version FROM schema_version").fetchone()[0] == 2
    old.close()
    with Catalog(path):  # up to date: no second backup
        pass
    assert len(list((tmp_path / "camera_catalog_backups").glob("*.sqlite"))) == 1


# -------------------------------------------------------------- placement
def test_place_follows_the_sensor_pixels_under_a_camera_roi() -> None:
    area = PartArea(100, 50, 200, 100)
    full = place(area, ROI(x=0, y=0, width=1000, height=500))
    assert full.note is None
    assert full.roi == FractionalROI(x=0.1, y=0.1, width=0.2, height=0.2)
    cropped = place(area, ROI(x=100, y=50, width=400, height=200))  # camera ROI moved
    assert cropped.roi == FractionalROI(x=0.0, y=0.0, width=0.5, height=0.5)
    half = place(area, ROI(x=200, y=0, width=500, height=500))
    assert half.roi is not None and half.note is not None and "50%" in half.note
    out = place(area, ROI(x=600, y=0, width=400, height=500))
    assert out.roi is None and out.note is not None and "whole image" in out.note


def test_crop_plane_keeps_the_bayer_phase() -> None:
    plane = np.arange(100 * 80, dtype=np.float64).reshape(80, 100)
    roi = FractionalROI(x=0.115, y=0.13, width=0.5, height=0.5)
    out = crop_plane(plane, roi)
    y0, x0 = divmod(int(out[0, 0]), 100)
    assert x0 % 2 == 0 and y0 % 2 == 0
    assert out.shape[0] % 2 == 0 and out.shape[1] % 2 == 0
    assert crop_plane(plane, None) is plane


# ---------------------------------------------------------------- service
def test_no_part_area_scores_the_whole_image(svc: TwoDService) -> None:
    p = svc.scoring_params(SIM)
    assert p.roi is None and p.roi_source == "none" and p.roi_note is None


def test_saved_part_area_scores_equal_a_hand_crop(svc: TwoDService, catalog: Path) -> None:
    w, h = _size(svc)
    area = ROI(x=w // 2, y=h // 4, width=w // 4, height=h // 2)  # part of the chart only
    saved = svc.set_part_area(SIM, area)
    assert (saved.x, saved.width) == (area.x, area.width)
    with Catalog(catalog) as c:  # the camera joined the catalog; a backup was made
        assert c.model_of(SIM) == MODEL
    assert list((catalog.parent / "camera_catalog_backups").glob("*part-area*"))
    [cap] = svc.capture(SIM)
    assert cap.scores.params["roi_source"] == "part_area"
    frame = svc.grab(SIM)
    crop = frame.data[area.y : area.y + area.height, area.x : area.x + area.width]
    by_hand = score_array(crop, frame.pixel_format, frame.bit_depth)
    whole = score_array(frame.data, frame.pixel_format, frame.bit_depth)
    scored = score_array(frame.data, frame.pixel_format, frame.bit_depth, svc.scoring_params(SIM))
    assert scored.metric("mean_level") == pytest.approx(by_hand.metric("mean_level"), abs=1e-6)
    differs = abs(scored.metric("mean_level") / whole.metric("mean_level") - 1)
    assert differs > 0.05  # the part is not the scene
    # a recorded capture is re-scored the same way
    again = svc.score_capture(cap.capture_id)
    assert again.params["roi_source"] == "part_area"
    assert again.metric("mean_level") == pytest.approx(cap.scores.metric("mean_level"), abs=1e-6)


def test_part_area_must_lie_in_the_image_and_needs_a_known_model(
    svc: TwoDService, make_service: Callable[..., TwoDService], tmp_path: Path
) -> None:
    w, _ = _size(svc)
    with pytest.raises(InvalidRequestError, match="inside the image"):
        svc.set_part_area(SIM, ROI(x=w - 10, y=0, width=100, height=100))
    svc.close(SIM)  # one holder at a time
    other = make_service(catalog=tmp_path / "empty.sqlite")
    other.open(SIM, "test")
    with pytest.raises(CatalogError, match="not in the catalog"):
        other.set_part_area(SIM, ROI(x=0, y=0, width=50, height=50))


def test_camera_roi_crop_moves_the_scored_window_with_the_part(svc: TwoDService) -> None:
    w, h = _size(svc)
    svc.set_part_area(SIM, ROI(x=w // 2, y=h // 2, width=w // 8, height=h // 8))
    res = svc.apply(
        SIM, CameraSettingsPatch(roi=ROI(x=w // 4, y=h // 4, width=w // 2, height=h // 2))
    )
    assert res.settings.roi == ROI(x=w // 4, y=h // 4, width=w // 2, height=h // 2), res
    p = svc.scoring_params(SIM)
    assert p.roi_source == "part_area" and p.roi_note is None
    assert p.roi == FractionalROI(x=0.5, y=0.5, width=0.25, height=0.25)
    # move the camera ROI off the part: the whole image is scored, and the report says so
    svc.apply(SIM, CameraSettingsPatch(roi=ROI(x=0, y=0, width=w // 4, height=h // 4)))
    p = svc.scoring_params(SIM)
    assert p.roi is None and p.roi_source == "none"
    assert p.roi_note is not None and "outside" in p.roi_note


def test_precedence_config_then_part_area_then_tag(
    make_service: Callable[..., TwoDService], catalog: Path
) -> None:
    config_roi = FractionalROI(x=0.0, y=0.0, width=0.5, height=0.5)
    tag_roi = FractionalROI(x=0.5, y=0.5, width=0.5, height=0.5)
    scoring = ScoringConfig.model_validate(
        {
            "default": {"roi": config_roi.model_dump()},
            "tags": {"shiny": {"roi": tag_roi.model_dump()}},
        }
    )
    s = make_service(catalog=catalog, scoring=scoring)
    s.open(SIM, "test")
    assert s.scoring_params(SIM).roi_source == "config"
    w, h = _size(s)
    s.set_part_area(SIM, ROI(x=0, y=0, width=w // 10, height=h // 10))
    p = s.scoring_params(SIM)
    assert p.roi_source == "part_area" and p.roi != config_roi
    t = s.scoring_params(SIM, tag="shiny")
    assert t.roi_source == "tag" and t.roi == tag_roi
    assert s.clear_part_area(SIM)
    assert s.scoring_params(SIM).roi_source == "config"
    assert isinstance(ScoringParamsOverride(roi=tag_roi), ScoringParamsOverride)


def test_no_catalog_file_is_never_created_by_scoring(
    make_service: Callable[..., TwoDService], tmp_path: Path
) -> None:
    path = tmp_path / "missing.sqlite"
    s = make_service(catalog=path)
    s.open(SIM, "test")
    s.capture(SIM)
    assert s.part_area(SIM) is None
    assert not s.clear_part_area(SIM)
    assert not path.exists()


def test_classic_tuner_steers_on_the_part(svc: TwoDService) -> None:
    w, h = _size(svc)
    svc.set_part_area(SIM, ROI(x=0, y=0, width=w // 4, height=h // 4))
    res = svc.tune(SIM, max_steps=12)
    assert res.best_capture_id is not None
    assert svc.score_capture(res.best_capture_id).params["roi_source"] == "part_area"
    assert svc.scoring_params(SIM).roi_source == "part_area"  # the tune kept the area


# ------------------------------------------------------------------- CLI
def test_cli_show_and_clear(
    svc: TwoDService, catalog: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    w, h = _size(svc)
    svc.set_part_area(SIM, ROI(x=8, y=8, width=w // 4, height=h // 4))
    svc.close(SIM)
    monkeypatch.setenv("CAMERA_LAB_CATALOG", str(catalog))
    assert catalog_path() == catalog
    runner = CliRunner()
    out = runner.invoke(app, ["--sim", "two-d", "part-area", "show", SIM])
    assert out.exit_code == 0, out.output
    assert f"x 8, y 8, {w // 4} × {h // 4} px" in out.output
    out = runner.invoke(app, ["--sim", "two-d", "part-area", "clear", SIM])
    assert out.exit_code == 0 and "part area cleared" in out.output
    out = runner.invoke(app, ["--sim", "two-d", "part-area", "show", SIM])
    assert "no part area" in out.output


# ------------------------------------------------------------ site light
def test_site_light_judges_the_part_and_says_so(svc: TwoDService, tmp_path: Path) -> None:
    from camera_lab.two_d.site.light import assess_light

    def run() -> dict[str, Any]:
        rep = assess_light(svc, SIM, sheet_root=tmp_path / "none", out_root=tmp_path / "out")
        return {c.name: c for c in rep.checks}

    whole = run()
    assert "area" not in whole
    w, h = _size(svc)
    svc.set_part_area(SIM, ROI(x=w // 2, y=0, width=w // 4, height=h // 4))
    part = run()
    assert part["area"].status == "info"
    assert part["snr"].data["level"] != pytest.approx(whole["snr"].data["level"], rel=0.02)
    # the 3 × 3 distribution stays whole-image
    grid = np.asarray(part["distribution"].data["grid_relative_to_centre"])
    assert grid == pytest.approx(
        np.asarray(whole["distribution"].data["grid_relative_to_centre"]), abs=0.03
    )
