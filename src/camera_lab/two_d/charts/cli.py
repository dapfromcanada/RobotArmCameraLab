"""``camera-lab charts``: the test-chart library (list, show, init, make PDFs)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Any

import typer

from camera_lab.cli_support import emit, handle_errors, state
from camera_lab.core.config import ConfigError
from camera_lab.core.paths import UnsafePathError
from camera_lab.two_d.charts.library import (
    FIELD_SET_ID,
    init_builtin,
    list_charts,
    load_chart,
    write_field_pdf,
    write_pdf,
)
from camera_lab.two_d.charts.models import ChartSpec
from camera_lab.two_d.errors import ConfigurationError, InvalidRequestError

app = typer.Typer(help="Test charts (chart library, printable PDFs).", no_args_is_help=True)

JsonOpt = Annotated[bool, typer.Option("--json", help="Machine-readable JSON on stdout.")]


@contextmanager
def _chart_errors() -> Iterator[None]:
    try:
        yield
    except (FileNotFoundError, UnsafePathError) as exc:
        raise InvalidRequestError(str(exc)) from None
    except ConfigError as exc:
        raise ConfigurationError(str(exc)) from None


def _summary(c: ChartSpec) -> str:
    tags = ",".join(str(t.id) for t in c.tags)
    return f"{c.id:26s} v{c.version:<3d} {c.type:12s} tags {tags:16s} {c.title}"


@app.command("list")
@handle_errors
def charts_list(json_out: JsonOpt = False) -> None:
    """List the charts in config/charts/.

    Example: camera-lab charts list
    """
    with _chart_errors():
        charts = list_charts()
    data = [
        c.model_dump(mode="json", include={"id", "version", "type", "title", "tags"})
        for c in charts
    ]
    emit(
        data,
        json_out,
        lambda: "\n".join(_summary(c) for c in charts) or "no charts (run: camera-lab charts init)",
    )


@app.command("show")
@handle_errors
def charts_show(chart_id: str, json_out: JsonOpt = False) -> None:
    """Show one chart (tags, features, notes).

    Example: camera-lab charts show step-wedge-letter-v1
    """
    with _chart_errors():
        c = load_chart(chart_id)

    def human() -> str:
        lines = [_summary(c), f"  paper {c.paper} {c.page_mm[0]} x {c.page_mm[1]} mm"]
        lines += [f"  tag {t.id}: {t.size_mm} mm at ({t.x_mm}, {t.y_mm})" for t in c.tags]
        lines += [f"  {f.kind} {f.name}" for f in c.features]
        lines += [f"  note: {n}" for n in c.notes]
        return "\n".join(lines)

    emit(c.model_dump(mode="json"), json_out, human)


@app.command("init")
@handle_errors
def charts_init(json_out: JsonOpt = False) -> None:
    """Write the YAML of the built-in charts that are missing (never overwrites).

    Example: camera-lab charts init
    """
    written = [str(p) for p in init_builtin()]
    emit(written, json_out, lambda: "\n".join(f"wrote {p}" for p in written) or "nothing to do")


@app.command("make")
@handle_errors
def charts_make(
    chart_id: Annotated[
        str | None,
        typer.Argument(
            help=f"Chart id, or {FIELD_SET_ID} for all nine field sheets (omit with --all)."
        ),
    ] = None,
    all_charts: Annotated[bool, typer.Option("--all", help="Make every chart.")] = False,
    out: Annotated[Path | None, typer.Option("--out", help="Output PDF (one chart only).")] = None,
    json_out: JsonOpt = False,
) -> None:
    """Write printable PDFs (default docs/charts/<id>.pdf). Print at 100 %, matte paper.
    The nine field-chart sheets always print together, as one PDF.

    Example: camera-lab charts make field-letter-v1
    """
    if (chart_id is None) == (not all_charts) or (out is not None and all_charts):
        raise InvalidRequestError("give one chart id (optionally with --out), or --all")
    with _chart_errors():
        if all_charts or chart_id == FIELD_SET_ID:
            charts = list_charts()
        else:
            charts = [load_chart(chart_id or "")]
    field = [c for c in charts if c.type == "field"]
    if chart_id == FIELD_SET_ID:
        if not field:
            raise InvalidRequestError("no field-chart sheets in the library (run: charts init)")
        charts = []
    paths = [str(write_pdf(c, out)) for c in charts if c.type != "field"]
    if field:
        paths.append(str(write_field_pdf(field, out if chart_id == FIELD_SET_ID else None)))
    emit(paths, json_out, lambda: "\n".join(f"wrote {p}" for p in paths))


def _score_line(rec: dict[str, object]) -> str:
    sc = rec.get("scores")
    if rec.get("type") == "field_map" and isinstance(sc, dict):
        return "\n".join(field_map_lines(sc))
    head = f"{rec.get('chart_id') or 'unknown tag ' + str(rec.get('tag_ids'))}"
    if not isinstance(sc, dict):
        return head
    if "error" in sc:
        return f"{head}: {sc['error']}"
    keys = {
        "step_wedge": ("steps_resolved", "clipped_steps", "monotonic", "r2_vs_nominal_grey"),
        "grey_patch": ("level", "snr", "wb_spread_pct", "contrast_vs_black"),
        "resolution": (
            "edges_measured",
            "mtf50_cyc_px",
            "mtf50_lp_mm",
            "mtf50_horizontal_cyc_px",
            "mtf50_vertical_cyc_px",
        ),
        "field": (
            "role",
            "edges_measured",
            "mtf50_cyc_px",
            "mtf50_sagittal_cyc_px",
            "mtf50_tangential_cyc_px",
            "r",
        ),
    }.get(str(rec.get("type")), ())
    return head + "  " + "  ".join(f"{k} {sc.get(k)}" for k in keys)


def _cell(row: dict[str, Any] | None) -> list[str]:
    """One sheet of the map as 4 short lines."""
    if row is None:
        return ["(not seen)", "", "", ""]
    sag, tan = row.get("mtf50_sagittal_cyc_px"), row.get("mtf50_tangential_cyc_px")
    hor, ver = row.get("mtf50_horizontal_cyc_px"), row.get("mtf50_vertical_cyc_px")
    split = f"S {sag} T {tan}" if sag or tan else f"H {hor} V {ver}"
    light = row.get("light_vs_centre")
    colour = ""
    if row.get("fringe_red_px") is not None or row.get("fringe_blue_px") is not None:
        colour = f"fringe R {row.get('fringe_red_px')} B {row.get('fringe_blue_px')} px"
    return [
        f"{row['role']}  r {row.get('r')}",
        f"MTF50 {row.get('mtf50_cyc_px')}  ({row.get('edges_measured')}/8)",
        split,
        (f"light {light}" if light is not None else "") + (f"  {colour}" if colour else ""),
    ]


def field_map_lines(fmap: dict[str, Any]) -> list[str]:
    """The field map as a 3 × 3 grid of text cells plus the summary."""
    rows = {r["role"]: r for r in fmap.get("sheets", [])}
    grid: tuple[tuple[str, str, str], ...] = (
        ("top-left", "top", "top-right"),
        ("left", "centre", "right"),
        ("bottom-left", "bottom", "bottom-right"),
    )
    width = 30
    out = ["field chart (MTF50 in cycles/px; S/T = sagittal/tangential; light vs centre)"]
    for line in grid:
        cells = [_cell(rows.get(role)) for role in line]
        out.append("+".join("-" * width for _ in line))
        out += ["|".join(c[i][:width].ljust(width) for c in cells) for i in range(4)]
    s = fmap.get("summary", {})
    out += [
        "",
        f"centre {s.get('centre_mtf50_cyc_px')}  corners mean {s.get('corner_mtf50_mean_cyc_px')}"
        f"  worst {s.get('corner_mtf50_worst_cyc_px')} ({s.get('corner_worst')})"
        f"  corners/centre {s.get('corner_vs_centre')}",
        f"corner spread {s.get('corner_spread')}  softer side {s.get('softer_side') or 'none'}"
        f"  corner astigmatism {s.get('corner_astigmatism')}",
        f"corner light vs centre (min) {s.get('corner_light_vs_centre_min')}"
        f"  edges {s.get('edges_measured')}/{s.get('edges_total')}"
        f"  missing {', '.join(s.get('sheets_missing') or []) or 'none'}"
        f"  clipped {', '.join(s.get('clipped_sheets') or []) or 'none'}",
    ]
    if s.get("corner_fringe_px_max") is not None or s.get("corner_colour_shading_pct_max"):
        out.append(
            f"colour: corner fringing max {s.get('corner_fringe_px_max')} px, "
            f"corner colour shading max {s.get('corner_colour_shading_pct_max')} %"
        )
    return out


@app.command("field")
@handle_errors
def charts_field(
    ctx: typer.Context,
    image: Annotated[Path, typer.Argument(help="TIFF file with the field chart in view.")],
    json_out: JsonOpt = False,
) -> None:
    """The field chart's whole-image map from a saved TIFF: sharpness, sagittal/tangential,
    light and colour per sheet, and the summary.

    Example: camera-lab charts field data/.../captures/0001.tiff
    """
    records = state(ctx).service(ctx).score_charts_file(image)
    fmap = next((r["scores"] for r in records if r.get("type") == "field_map"), None)
    if fmap is None:
        raise InvalidRequestError("no field-chart sheet found in the image")
    emit(fmap, json_out, lambda: "\n".join(field_map_lines(fmap)))


@app.command("field-plan")
@handle_errors
def charts_field_plan(
    model: Annotated[str, typer.Option(help="Camera model (its white paper gives the sensor).")],
    lens: Annotated[str | None, typer.Option(help="Lens id in the catalog.")] = None,
    distance_mm: Annotated[
        float | None, typer.Option(help="Board distance from the lens (default: the planned one).")
    ] = None,
    focal_mm: Annotated[
        float | None, typer.Option(help="Focal length, if not from --lens.")
    ] = None,
    pixel_um: Annotated[
        float | None, typer.Option(help="Pixel pitch, if not in the paper.")
    ] = None,
    width_px: Annotated[int | None, typer.Option(help="Sensor width, if not in the paper.")] = None,
    height_px: Annotated[
        int | None, typer.Option(help="Sensor height, if not in the paper.")
    ] = None,
    json_out: JsonOpt = False,
) -> None:
    """Where to put the field chart's three boards for a camera + lens: the distance, the
    spacing of the boards, the view and how far out the corner squares land.

    Example: camera-lab charts field-plan --model TRI089S-M --lens computar_f_1_2_8_1_1_c
    """
    from camera_lab.two_d.camera_models import load_model_sheet
    from camera_lab.two_d.catalog import Catalog
    from camera_lab.two_d.charts.field import field_plan

    sheet = load_model_sheet(model)
    sensor = sheet.sensor if sheet is not None else None
    pixels = (
        (width_px, height_px)
        if width_px and height_px
        else (sensor.pixels if sensor is not None else None)
    )
    pitch = pixel_um or (sensor.pixel_um if sensor is not None else None)
    focal = focal_mm
    if focal is None and lens is not None:
        with Catalog() as cat:
            info = cat.lens(lens)
        if info is None:
            raise InvalidRequestError(
                f"no lens {lens!r} in the catalog (see: two-d catalog lenses)"
            )
        focal = info.focal_mm
    missing = [
        n
        for n, v in (("sensor size (--width-px/--height-px)", pixels), ("--pixel-um", pitch))
        if not v
    ]
    if not focal:
        missing.append("focal length (--lens or --focal-mm)")
    if missing:
        raise InvalidRequestError(
            f"missing for {model}: {', '.join(missing)} (not in its white paper)"
        )
    if pixels is None or pitch is None or focal is None:  # for the type checker
        raise InvalidRequestError("sensor size, pixel pitch and focal length are needed")
    try:
        plan = field_plan((int(pixels[0]), int(pixels[1])), float(pitch), float(focal), distance_mm)
    except ValueError as exc:
        raise InvalidRequestError(str(exc)) from None

    def human() -> str:
        c = plan["board_centres_mm"][2]
        lines = [
            f"{model}, {focal:g} mm lens, sensor {pixels[0]} x {pixels[1]} px at {pitch:g} um",
            f"distance (lens to boards): {plan['distance_mm']} mm",
            f"view at that distance: {plan['view_mm'][0]} x {plan['view_mm'][1]} mm, "
            f"{plan['px_per_mm']} px/mm, squares {plan['square_px']} px",
            f"boards: centre board in the middle, side boards {c} mm left and right of it "
            f"(centre to centre), {plan['board_gap_mm']} mm gaps, "
            f"{plan['outer_edge_to_edge_mm']} mm outer edge to outer edge",
            f"corner squares land at r = {plan['corner_squares_r']} (1 = image corner)",
        ]
        return "\n".join(lines + [f"WARNING: {w}" for w in plan["warnings"]])

    emit(plan, json_out, human)


@app.command("score")
@handle_errors
def charts_score(
    ctx: typer.Context,
    image: Annotated[Path, typer.Argument(help="TIFF file (e.g. a capture).")],
    json_out: JsonOpt = False,
) -> None:
    """Find the library charts in a TIFF and score them (deterministic).

    Example: camera-lab charts score data/.../captures/0001.tiff
    """
    records = state(ctx).service(ctx).score_charts_file(image)
    emit(records, json_out, lambda: "\n".join(_score_line(r) for r in records) or "no charts found")
