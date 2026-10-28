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
    FieldRole,
    PatchFeature,
    SlantedSquareFeature,
    StarFeature,
    TagSpec,
)
from camera_lab.two_d.charts.pdf import render_pdf, render_pdf_pages

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
#: Field chart (Phase 10 step 7): nine landscape Letter sheets printed as one PDF.
FIELD_SET_ID = "field-letter-v1"
#: role → (tag id, column, row): column / row −1, 0, +1 = left / middle / right of the view
#: (top / middle / bottom); the squares sit toward that side of the sheet.
FIELD_ROLES: dict[FieldRole, tuple[int, int, int]] = {
    "centre": (130, 0, 0),
    "top-left": (131, -1, -1),
    "top": (132, 0, -1),
    "top-right": (133, 1, -1),
    "right": (134, 1, 0),
    "bottom-right": (135, 1, 1),
    "bottom": (136, 0, 1),
    "bottom-left": (137, -1, 1),
    "left": (138, -1, 0),
}
FIELD_SQUARE_MM = 50.0
FIELD_SQUARE_ANGLE = 5.0
FIELD_SQUARE_PITCH_MM = 65.0  # centre to centre of the two squares
FIELD_SQUARE_INSET_MM = 42.0  # paper edge to the outer square's centre (~15 mm clear)
FIELD_SQUARE_LOW_Y_MM = 140.0  # bottom-row sheets: square centres, clear of the footer
FIELD_STEPS = (0.0, 0.2, 0.4, 0.6, 0.8, 1.0)
FIELD_STEP_MM = 18.0
FIELD_GAP_MM = 15.0
_BOARD = {-1: "LEFT", 0: "CENTRE", 1: "RIGHT"}
_SLOT = {-1: "top", 0: "middle", 1: "bottom"}


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


def _field(role: FieldRole) -> ChartSpec:
    """One field-chart sheet: two slanted squares toward the outside of the camera view, the
    tag and a 6-step grey strip on the inner side, the role in large print."""
    tag_id, col, row = FIELD_ROLES[role]
    w, _h = PAPER_MM["letter-landscape"]
    half = FIELD_SQUARE_PITCH_MM / 2
    sq_x = {
        -1: (FIELD_SQUARE_INSET_MM, FIELD_SQUARE_INSET_MM + FIELD_SQUARE_PITCH_MM),
        0: (w / 2 - half, w / 2 + half),
        1: (w - FIELD_SQUARE_INSET_MM - FIELD_SQUARE_PITCH_MM, w - FIELD_SQUARE_INSET_MM),
    }[col]
    low = row == 1
    sq_y = FIELD_SQUARE_LOW_Y_MM if low else FIELD_SQUARE_INSET_MM
    tag_y = 20.0 if low else 85.0
    tag_x = w - TAG_XY - TAG_MM if col == -1 else TAG_XY
    strip_w = len(FIELD_STEPS) * FIELD_STEP_MM
    strip_x = tag_x - FIELD_GAP_MM - strip_w if col == -1 else tag_x + TAG_MM + FIELD_GAP_MM
    squares = tuple(
        SlantedSquareFeature(
            name=f"edge-{i + 1}",
            cx_mm=round(x, 3),
            cy_mm=sq_y,
            size_mm=FIELD_SQUARE_MM,
            angle_deg=FIELD_SQUARE_ANGLE,
        )
        for i, x in enumerate(sq_x)
    )
    steps = tuple(
        PatchFeature(
            name=f"step-{i + 1}",
            x_mm=round(strip_x + i * FIELD_STEP_MM, 3),
            y_mm=tag_y,
            w_mm=FIELD_STEP_MM,
            h_mm=FIELD_STEP_MM,
            grey=g,
        )
        for i, g in enumerate(FIELD_STEPS)
    )
    notes = [
        f"{_BOARD[col]} board, {_SLOT[row]} sheet. The squares point to the outside of the "
        "camera view.",
        "Glue flat (spray adhesive, no tape). All boards in one plane, facing the camera, "
        "against one straight rail. Every tag in view.",
    ]
    if role == "centre":
        notes += [
            "Boards, left to right: LEFT (top-left, left, bottom-left), CENTRE (top, centre, "
            "bottom), RIGHT (top-right, right, bottom-right).",
            "Distance and spacing of the boards for a camera + lens: "
            "camera-lab charts field-plan <camera> --distance-mm N",
        ]
    return ChartSpec(
        id=f"field-{role}-letter-v1",
        type="field",
        version=1,
        title=f"Field chart sheet: {role.upper()} ({FIELD_SET_ID})",
        paper="letter-landscape",
        role=role,
        tags=(TagSpec(id=tag_id, x_mm=tag_x, y_mm=tag_y, size_mm=TAG_MM),),
        features=(*squares, *steps),
        notes=tuple(notes),
    )


def field_charts() -> list[ChartSpec]:
    """The nine sheets of the field chart, centre first."""
    return [_field(role) for role in FIELD_ROLES]


def builtin_charts() -> list[ChartSpec]:
    """The first Letter charts (Phase 10 step 1) and the field chart sheets (step 7)."""
    return [_step_wedge(), _resolution(), _grey_patch(), _tag_card(), *field_charts()]


def write_field_pdf(sheets: list[ChartSpec], out: Path | None = None) -> Path:
    """All field-chart sheets in one PDF (default ``APP/docs/charts/field-letter-v1.pdf``)."""
    path = out if out is not None else safe_join(pdf_dir(), f"{FIELD_SET_ID}.pdf")
    path.parent.mkdir(parents=True, exist_ok=True)
    order = list(FIELD_ROLES)
    sheets = sorted(sheets, key=lambda c: order.index(c.role) if c.role in order else len(order))
    pages = [(chart_drawing(s), s.page_mm) for s in sheets]
    path.write_bytes(render_pdf_pages(pages, f"Field chart, nine sheets ({FIELD_SET_ID})"))
    return path


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
