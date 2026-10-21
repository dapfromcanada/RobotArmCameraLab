"""The chart library: ``APP/config/charts/<chart_id>.yaml`` (+ PDFs in ``APP/docs/charts``).

The YAML files are the source of truth (human-editable: notes, print date).
:func:`builtin_charts` holds the designs of the first Letter charts; ``init``
writes their YAML only when it is missing, never over an existing file.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from camera_lab.core.config import ConfigError, load_yaml_model
from camera_lab.core.paths import APP_ROOT, app_config_dir, safe_join, validate_slug
from camera_lab.two_d.charts.draw import chart_drawing
from camera_lab.two_d.charts.models import (
    PAPER_MM,
    ChartSpec,
    PatchFeature,
    SlantedSquareFeature,
    StarFeature,
    TagSpec,
)
from camera_lab.two_d.charts.pdf import render_pdf

CHARTS_DIRNAME = "charts"
LETTER_W, _LETTER_H = PAPER_MM["letter"]
TAG_MM = 40.0
TAG_XY = 15.0  # top-left tag position on the single-tag charts
WEDGE_STEPS = 20
WEDGE_COLS = 5
WEDGE_PATCH_MM = 34.0
WEDGE_TOP_MM = 72.0
STAR_SPOKES = 36
CARD_TAG_MM = 60.0
CARD_IDS = (120, 121, 122, 123)


def charts_dir() -> Path:
    """``APP/config/charts``."""
    return app_config_dir() / CHARTS_DIRNAME


def pdf_dir() -> Path:
    """``APP/docs/charts`` (generated, printable PDFs)."""
    return APP_ROOT / "docs" / CHARTS_DIRNAME


def load_chart(chart_id: str, root: Path | None = None) -> ChartSpec:
    """Load one chart.

    Raises:
        FileNotFoundError: no such chart.
        ConfigError: the file is invalid or its id does not match the file name.
    """
    validate_slug(chart_id)
    path = safe_join(root if root is not None else charts_dir(), f"{chart_id}.yaml")
    if not path.exists():
        raise FileNotFoundError(f"no chart {chart_id!r} in {path.parent}")
    spec = load_yaml_model(path, ChartSpec)
    if spec.id != chart_id:
        raise ConfigError(path, f"id {spec.id!r} does not match file name {chart_id!r}")
    return spec


def list_charts(root: Path | None = None) -> list[ChartSpec]:
    """All charts, sorted by id. Tag ids must be unique across the library.

    Raises:
        ConfigError: an invalid file, or a tag id used by two charts.
    """
    base = root if root is not None else charts_dir()
    if not base.is_dir():
        return []
    charts = [load_chart(p.stem, base) for p in sorted(base.glob("*.yaml"))]
    owner: dict[int, str] = {}
    for c in charts:
        for t in c.tags:
            if t.id in owner:
                raise ConfigError(
                    base / f"{c.id}.yaml", f"tag {t.id} is already used by chart {owner[t.id]!r}"
                )
            owner[t.id] = c.id
    return charts


def tag_owner(root: Path | None = None) -> dict[int, ChartSpec]:
    """Map tag id → chart (for detection, Phase 10 step 2)."""
    return {t.id: c for c in list_charts(root) for t in c.tags}


def make_pdf(spec: ChartSpec) -> bytes:
    """The printable PDF of ``spec``."""
    return render_pdf(chart_drawing(spec), spec.page_mm, f"{spec.title} ({spec.id})")


def write_pdf(spec: ChartSpec, out: Path | None = None) -> Path:
    """Write the PDF (default ``APP/docs/charts/<id>.pdf``) and return its path."""
    path = out if out is not None else safe_join(pdf_dir(), f"{spec.id}.pdf")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(make_pdf(spec))
    return path


# ------------------------------------------------------------ built-in designs
def _single_tag(tag_id: int) -> tuple[TagSpec, ...]:
    return (TagSpec(id=tag_id, x_mm=TAG_XY, y_mm=TAG_XY, size_mm=TAG_MM),)


def _step_wedge() -> ChartSpec:
    left = (LETTER_W - WEDGE_COLS * WEDGE_PATCH_MM) / 2
    steps = []
    for i in range(WEDGE_STEPS):
        row, col = divmod(i, WEDGE_COLS)
        steps.append(
            PatchFeature(
                name=f"step-{i + 1:02d}",
                x_mm=round(left + col * WEDGE_PATCH_MM, 3),
                y_mm=WEDGE_TOP_MM + row * WEDGE_PATCH_MM,
                w_mm=WEDGE_PATCH_MM,
                h_mm=WEDGE_PATCH_MM,
                grey=round(i / (WEDGE_STEPS - 1), 4),
            )
        )
    return ChartSpec(
        id="step-wedge-letter-v1",
        type="step_wedge",
        version=1,
        title="Grey step wedge, 20 steps",
        tags=_single_tag(100),
        features=tuple(steps),
        notes=(
            "Steps row by row from the top-left: device grey 0.00 (black) to 1.00 (paper white), "
            "linear steps. Office prints are nominal: use for relative tests.",
        ),
    )


def _resolution() -> ChartSpec:
    return ChartSpec(
        id="resolution-letter-v1",
        type="resolution",
        version=1,
        title="Resolution: slanted edges + Siemens star",
        tags=_single_tag(101),
        features=(
            StarFeature(name="star", cx_mm=140.0, cy_mm=75.0, radius_mm=52.0, spokes=STAR_SPOKES),
            SlantedSquareFeature(
                name="edge-left", cx_mm=60.0, cy_mm=175.0, size_mm=50.0, angle_deg=5.0
            ),
            SlantedSquareFeature(
                name="edge-right", cx_mm=150.0, cy_mm=175.0, size_mm=50.0, angle_deg=5.0
            ),
        ),
        notes=(
            "Slanted squares (5°) give horizontal and vertical edges for MTF50; the star shows "
            "resolution in all directions. Fixed-lens cameras: set focus once, then compare.",
        ),
    )


def _grey_patch() -> ChartSpec:
    size = 140.0
    return ChartSpec(
        id="grey-patch-letter-v1",
        type="grey_patch",
        version=1,
        title="Neutral grey patch (device grey 0.50)",
        tags=_single_tag(102),
        features=(
            PatchFeature(
                name="grey",
                x_mm=round((LETTER_W - size) / 2, 3),
                y_mm=68.0,
                w_mm=size,
                h_mm=size,
                grey=0.5,
            ),
            PatchFeature(name="black-ref", x_mm=150.0, y_mm=25.0, w_mm=30.0, h_mm=30.0, grey=0.0),
        ),
        notes=(
            "Nominal neutral for exposure and white-balance checks. For absolute values use a "
            "bought 18 % grey card marked with a tag card.",
        ),
    )


def _tag_card() -> ChartSpec:
    gap = LETTER_W - 2 * 30.0 - 2 * CARD_TAG_MM
    xs = (30.0, round(30.0 + CARD_TAG_MM + gap, 3))
    ys = (30.0, 130.0)
    tags = tuple(
        TagSpec(id=tid, x_mm=xs[i % 2], y_mm=ys[i // 2], size_mm=CARD_TAG_MM)
        for i, tid in enumerate(CARD_IDS)
    )
    return ChartSpec(
        id="tag-card-letter-v1",
        type="tag_card",
        version=1,
        title="Tag cards 120-123 (cut out, keep 10 mm white border)",
        tags=tags,
        notes=(
            "Markers for flat-field panels, part trays and bought charts. When a tag is assigned "
            "to another chart, remove it here (tag ids are unique in the library).",
        ),
    )


def builtin_charts() -> list[ChartSpec]:
    """The first Letter charts (Phase 10 step 1)."""
    return [_step_wedge(), _resolution(), _grey_patch(), _tag_card()]


def init_builtin(root: Path | None = None) -> list[Path]:
    """Write the YAML of every built-in chart that has no file yet; return the new paths."""
    base = root if root is not None else charts_dir()
    base.mkdir(parents=True, exist_ok=True)
    written = []
    for spec in builtin_charts():
        path = safe_join(base, f"{spec.id}.yaml")
        if path.exists():
            continue
        header = (
            f"# camera_lab test chart: {spec.title}\n"
            "# Generated by `camera-lab charts init`; human-editable (notes, print date).\n"
            "# Coordinates: mm on the page, origin top-left, y down. Grey: PDF device grey.\n"
        )
        body = yaml.safe_dump(spec.model_dump(mode="json"), sort_keys=False, allow_unicode=True)
        path.write_text(header + body, encoding="utf-8")
        written.append(path)
    return written
