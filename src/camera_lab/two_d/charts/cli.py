"""``camera-lab charts``: the test-chart library (list, show, init, make PDFs)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated

import typer

from camera_lab.cli_support import emit, handle_errors, state
from camera_lab.core.config import ConfigError
from camera_lab.core.paths import UnsafePathError
from camera_lab.two_d.charts.library import init_builtin, list_charts, load_chart, write_pdf
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
    chart_id: Annotated[str | None, typer.Argument(help="Chart id (omit with --all).")] = None,
    all_charts: Annotated[bool, typer.Option("--all", help="Make every chart.")] = False,
    out: Annotated[Path | None, typer.Option("--out", help="Output PDF (one chart only).")] = None,
    json_out: JsonOpt = False,
) -> None:
    """Write printable PDFs (default docs/charts/<id>.pdf). Print at 100 %, matte paper.

    Example: camera-lab charts make --all
    """
    if (chart_id is None) == (not all_charts) or (out is not None and all_charts):
        raise InvalidRequestError("give one chart id (optionally with --out), or --all")
    with _chart_errors():
        charts = list_charts() if all_charts else [load_chart(chart_id or "")]
    paths = [str(write_pdf(c, out)) for c in charts]
    emit(paths, json_out, lambda: "\n".join(f"wrote {p}" for p in paths))


def _score_line(rec: dict[str, object]) -> str:
    sc = rec.get("scores")
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
    }.get(str(rec.get("type")), ())
    return head + "  " + "  ".join(f"{k} {sc.get(k)}" for k in keys)


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
