"""Phase 10 step 1: chart library, drawing and PDF generation."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from camera_lab.cli import app
from camera_lab.core.config import ConfigError
from camera_lab.two_d.charts import (
    TAG_ID_MAX,
    TAG_ID_MIN,
    builtin_charts,
    field_charts,
    init_builtin,
    list_charts,
    load_chart,
    make_pdf,
    tag_owner,
)
from camera_lab.two_d.charts.detect import charts_from_tags, detect_tags
from camera_lab.two_d.charts.draw import SCALE_BAR_MM, Rect, Text, chart_drawing, rasterize
from camera_lab.two_d.charts.models import (
    PAPER_MM,
    ChartSpec,
    PatchFeature,
    SlantedSquareFeature,
    TagSpec,
)
from camera_lab.two_d.charts.pdf import PT_PER_MM, render_pdf
from camera_lab.two_d.charts.scores import score_resolution

PAGE_LANDSCAPE = PAPER_MM["letter-landscape"]
PX_PER_MM = 4.0
runner = CliRunner()


def _detect(img: np.ndarray) -> dict[int, float]:
    """Tag id → mean side length in pixels."""
    det = cv2.aruco.ArucoDetector(cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_APRILTAG_36h11))
    corners, ids, _ = det.detectMarkers(img)
    if ids is None:
        return {}
    out = {}
    for c, i in zip(corners, ids.ravel(), strict=True):
        pts = c[0]
        out[int(i)] = float(np.mean([np.linalg.norm(pts[k] - pts[(k + 1) % 4]) for k in range(4)]))
    return out


def _spec(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "test-chart-v1",
        "type": "grey_patch",
        "version": 1,
        "title": "test",
        "tags": [{"id": 150, "x_mm": 20, "y_mm": 20, "size_mm": 40}],
        "features": [
            {
                "kind": "patch",
                "name": "p",
                "x_mm": 30,
                "y_mm": 100,
                "w_mm": 50,
                "h_mm": 50,
                "grey": 0.5,
            }
        ],
    }
    base.update(kw)
    return base


# ------------------------------------------------------------------ models
def test_builtin_charts_are_valid_and_tags_unique() -> None:
    charts = builtin_charts()
    assert [c.id for c in charts] == [
        "step-wedge-letter-v1",
        "resolution-letter-v1",
        "grey-patch-letter-v1",
        "tag-card-letter-v1",
        *(c.id for c in field_charts()),
    ]
    ids = [t.id for c in charts for t in c.tags]
    assert len(ids) == len(set(ids))
    assert all(TAG_ID_MIN <= i <= TAG_ID_MAX for i in ids)
    wedge = charts[0]
    greys = [f.grey for f in wedge.features if isinstance(f, PatchFeature)]
    assert len(greys) == 20 and greys[0] == 0.0 and greys[-1] == 1.0
    assert greys == sorted(greys)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (
            {"tags": [{"id": 99, "x_mm": 20, "y_mm": 20, "size_mm": 40}]},
            "greater than or equal to 100",
        ),
        (
            {"tags": [{"id": 200, "x_mm": 20, "y_mm": 20, "size_mm": 40}]},
            "less than or equal to 199",
        ),
        ({"tags": [{"id": 150, "x_mm": 1, "y_mm": 20, "size_mm": 40}]}, "quiet zone"),
        (
            {
                "tags": [
                    {"id": 150, "x_mm": 20, "y_mm": 20, "size_mm": 40},
                    {"id": 150, "x_mm": 120, "y_mm": 20, "size_mm": 40},
                ]
            },
            "duplicate tag ids",
        ),
        (
            {
                "features": [
                    {
                        "kind": "patch",
                        "name": "p",
                        "x_mm": 60,
                        "y_mm": 40,
                        "w_mm": 20,
                        "h_mm": 20,
                        "grey": 0,
                    }
                ]
            },
            "overlaps tag 150",
        ),
        (
            {
                "features": [
                    {
                        "kind": "patch",
                        "name": "p",
                        "x_mm": 200,
                        "y_mm": 100,
                        "w_mm": 50,
                        "h_mm": 50,
                        "grey": 0,
                    }
                ]
            },
            "does not fit",
        ),
        ({"id": "../evil"}, "chart id must match"),
        ({"extra": 1}, "Extra inputs"),
    ],
)
def test_invalid_specs_rejected(change: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=re.escape(message)):
        ChartSpec.model_validate(_spec(**change))


# ----------------------------------------------------------------- library
def test_init_list_load(tmp_path: Path) -> None:
    written = init_builtin(tmp_path)
    assert len(written) == 4 + 9  # + the nine field-chart sheets
    assert init_builtin(tmp_path) == []  # never overwrites
    charts = list_charts(tmp_path)
    assert [c.id for c in charts] == sorted(c.id for c in builtin_charts())
    assert load_chart("resolution-letter-v1", tmp_path) == next(
        c for c in builtin_charts() if c.id == "resolution-letter-v1"
    )
    assert set(tag_owner(tmp_path)) == {100, 101, 102, 120, 121, 122, 123, *range(130, 139)}


def test_library_errors(tmp_path: Path) -> None:
    init_builtin(tmp_path)
    with pytest.raises(FileNotFoundError):
        load_chart("no-such-chart", tmp_path)
    with pytest.raises(ValueError):
        load_chart("../escape", tmp_path)
    renamed = tmp_path / "other-id.yaml"
    renamed.write_text((tmp_path / "grey-patch-letter-v1.yaml").read_text())
    with pytest.raises(ConfigError, match="does not match file name"):
        load_chart("other-id", tmp_path)
    renamed.unlink()
    clash = _spec(id="clash-v1", tags=[{"id": 100, "x_mm": 20, "y_mm": 20, "size_mm": 40}])
    (tmp_path / "clash-v1.yaml").write_text(json.dumps(clash))  # JSON is valid YAML
    with pytest.raises(ConfigError, match="tag 100 is already used"):
        list_charts(tmp_path)


def test_config_charts_match_the_designs() -> None:
    """The committed config/charts files load and keep the designed layout."""
    designs = {c.id: c for c in builtin_charts()}
    for c in list_charts():
        if c.id in designs:
            assert c.model_dump(exclude={"notes"}) == designs[c.id].model_dump(exclude={"notes"})


# ----------------------------------------------------------------- drawing
@pytest.mark.parametrize("chart", builtin_charts(), ids=lambda c: c.id)
def test_rendered_tags_are_detected_with_the_right_id_and_size(chart: ChartSpec) -> None:
    found = _detect(rasterize(chart, PX_PER_MM))
    assert set(found) == {t.id for t in chart.tags}
    for t in chart.tags:
        assert found[t.id] == pytest.approx(t.size_mm * PX_PER_MM, rel=0.02)


def test_drawing_has_label_and_scale_bar() -> None:
    chart = builtin_charts()[0]
    shapes = chart_drawing(chart)
    texts = " ".join(s.text for s in shapes if isinstance(s, Text))
    assert chart.id in texts and "tag 100" in texts and "50 mm" in texts
    assert any(isinstance(s, Rect) and s.w == SCALE_BAR_MM for s in shapes)


# --------------------------------------------------------------------- pdf
def test_pdf_structure_is_valid_and_deterministic() -> None:
    chart = builtin_charts()[1]
    pdf = make_pdf(chart)
    assert pdf == make_pdf(chart)  # deterministic: no dates
    assert pdf.startswith(b"%PDF-1.4") and pdf.rstrip().endswith(b"%%EOF")
    assert b"/MediaBox [0 0 612 792]" in pdf  # Letter in points
    start = int(re.search(rb"startxref\n(\d+)", pdf).group(1))  # type: ignore[union-attr]
    assert pdf[start:].startswith(b"xref")
    offsets = [int(m) for m in re.findall(rb"(\d{10}) 00000 n", pdf)]
    for i, off in enumerate(offsets, start=1):
        assert pdf[off:].startswith(f"{i} 0 obj".encode())
    length = int(re.search(rb"/Length (\d+)", pdf).group(1))  # type: ignore[union-attr]
    body = pdf.split(b"stream\n", 1)[1]
    assert body[length:].startswith(b"endstream")


def test_pdf_text_is_escaped_and_scaled() -> None:
    pdf = render_pdf([Text(10, 20, 8, "a (b) \\ c")], (100.0, 50.0), "t (x)")
    assert b"(a \\(b\\) \\\\ c) Tj" in pdf
    assert b"/Title (t \\(x\\))" in pdf
    assert f"{10 * PT_PER_MM:.3f}".rstrip("0").encode() in pdf


@pytest.mark.skipif(shutil.which("pdftoppm") is None, reason="poppler pdftoppm not installed")
def test_pdf_renders_with_poppler_and_tags_detect(tmp_path: Path) -> None:
    """Independent check: a real PDF renderer draws the tags where the spec says."""
    dpi = 100
    for chart in builtin_charts():
        pdf = tmp_path / f"{chart.id}.pdf"
        pdf.write_bytes(make_pdf(chart))
        subprocess.run(
            [
                str(shutil.which("pdftoppm")),
                "-r",
                str(dpi),
                "-gray",
                "-png",
                "-singlefile",
                str(pdf),
                str(tmp_path / chart.id),
            ],
            check=True,
            capture_output=True,
            timeout=60,
        )
        img = cv2.imread(str(tmp_path / f"{chart.id}.png"), cv2.IMREAD_GRAYSCALE)
        w_mm, h_mm = chart.page_mm  # Letter: 11 x 8.5 in at 100 dpi, portrait or landscape
        assert img.shape == (round(h_mm / 25.4 * dpi), round(w_mm / 25.4 * dpi))
        found = _detect(img)
        assert set(found) == {t.id for t in chart.tags}
        for t in chart.tags:
            assert found[t.id] == pytest.approx(t.size_mm / 25.4 * dpi, rel=0.03)


# --------------------------------------------------------------------- cli
def test_cli_list_show_make(tmp_path: Path) -> None:
    res = runner.invoke(app, ["charts", "list", "--json"])
    assert res.exit_code == 0, res.output
    ids = {c["id"] for c in json.loads(res.stdout)["result"]}
    assert {"step-wedge-letter-v1", "tag-card-letter-v1"} <= ids
    res = runner.invoke(app, ["charts", "show", "step-wedge-letter-v1"])
    assert res.exit_code == 0 and "tag 100" in res.stdout
    out = tmp_path / "w.pdf"
    res = runner.invoke(app, ["charts", "make", "step-wedge-letter-v1", "--out", str(out)])
    assert res.exit_code == 0, res.output
    assert out.read_bytes() == make_pdf(load_chart("step-wedge-letter-v1"))


@pytest.mark.parametrize(
    "args",
    [
        ["charts", "make"],
        ["charts", "make", "step-wedge-letter-v1", "--all"],
        ["charts", "make", "--all", "--out", "x.pdf"],
        ["charts", "make", "no-such-chart"],
        ["charts", "show", "../evil"],
    ],
)
def test_cli_bad_requests_exit_2(args: list[str]) -> None:
    res = runner.invoke(app, args)
    assert res.exit_code == 2, res.output


def test_tagspec_quiet_box() -> None:
    t = TagSpec(id=100, x_mm=16, y_mm=16, size_mm=40)
    assert t.quiet_box() == (11.0, 11.0, 61.0, 61.0)


# ------------------------------------------------------------- field chart
def test_field_chart_has_nine_sheets_with_squares_toward_the_outside() -> None:
    sheets = field_charts()
    assert sheets[0].role == "centre" and len(sheets) == 9
    assert sorted(t.id for c in sheets for t in c.tags) == list(range(130, 139))
    w, h = PAGE_LANDSCAPE
    for c in sheets:
        assert c.type == "field" and c.paper == "letter-landscape" and c.page_mm == (w, h)
        squares = [f for f in c.features if isinstance(f, SlantedSquareFeature)]
        steps = [f for f in c.features if isinstance(f, PatchFeature)]
        assert len(squares) == 2 and len(steps) == 6
        cx = sum(f.cx_mm for f in squares) / 2
        cy = sum(f.cy_mm for f in squares) / 2
        role = c.role or ""
        if "left" in role:
            assert cx < w / 3
        elif "right" in role:
            assert cx > 2 * w / 3
        else:
            assert w / 3 < cx < 2 * w / 3
        assert (cy > h / 2) == role.startswith("bottom")


def test_field_role_is_required_for_field_sheets_only() -> None:
    sheet = field_charts()[0].model_dump()
    with pytest.raises(ValidationError, match="needs a role"):
        ChartSpec.model_validate({**sheet, "role": None})
    with pytest.raises(ValidationError, match="needs a role"):
        ChartSpec.model_validate({**_spec(), "role": "centre"})


def test_field_pdf_is_one_file_of_nine_landscape_pages(tmp_path: Path) -> None:
    out = tmp_path / "field.pdf"
    res = runner.invoke(app, ["charts", "make", "field-letter-v1", "--out", str(out)])
    assert res.exit_code == 0, res.output
    pdf = out.read_bytes()
    assert b"/Count 9" in pdf and pdf.count(b"/Type /Page ") == 9
    assert pdf.count(b"/MediaBox [0 0 792 612]") == 9
    for c in field_charts():
        assert c.role is not None and c.role.upper().encode() in pdf


def test_field_sheet_tag_detects_and_all_edges_measure() -> None:
    sheet = field_charts()[1]  # top-left
    page = cv2.GaussianBlur(rasterize(sheet, 4.0), (0, 0), 1.0)
    found = _detect(page)
    assert set(found) == {sheet.tags[0].id}
    [det] = charts_from_tags(detect_tags(page), {sheet.tags[0].id: sheet}).charts
    r = score_resolution(page / 255.0, det, sheet)
    assert r["edges_measured"] == 8
