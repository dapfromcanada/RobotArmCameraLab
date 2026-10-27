"""camera-lab command-line entry point (Typer root app).

The CLI holds no business logic; it calls :class:`~camera_lab.two_d.service.TwoDService`.
Results go to stdout (``--json`` for machine-readable output); logs to stderr.

Exit codes: 0 OK, 1 unexpected error, 2 usage error, 3 camera not found/busy,
4 rejected by limits/range, 5 vendor SDK missing, 6 capture timeout.

Config precedence: command-line flags > environment (``CAMERA_LAB_*``) >
config files in ``APP/config/`` > built-in defaults.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from camera_lab import __version__
from camera_lab.cli_support import (
    EXIT_REJECTED,
    CliState,
    emit,
    handle_errors,
    state,
    to_json,
)
from camera_lab.core.logging import configure_logging
from camera_lab.two_d.charts.cli import app as charts_app
from camera_lab.two_d.cli import app as two_d_app

app = typer.Typer(
    name="camera-lab",
    help="Check and tune the robot cell's cameras and lighting.",
    no_args_is_help=True,
    add_completion=False,
)
light_app = typer.Typer(
    help="Light control (through the safety limits). Lights turn off when the command exits.",
    no_args_is_help=True,
)
limits_app = typer.Typer(help="Safety limits (read-only).", no_args_is_help=True)

app.add_typer(two_d_app, name="two-d")
app.add_typer(light_app, name="light")
app.add_typer(limits_app, name="limits")
app.add_typer(charts_app, name="charts")

JsonOpt = Annotated[bool, typer.Option("--json", help="Machine-readable JSON on stdout.")]


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit()


@app.callback()
def main(
    ctx: typer.Context,
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="Print the camera_lab version and exit.",
        ),
    ] = False,
    quiet: Annotated[bool, typer.Option("--quiet", "-q", help="Only log errors.")] = False,
    log_level: Annotated[
        str | None,
        typer.Option(
            "--log-level",
            help="DEBUG/INFO/WARNING/ERROR/CRITICAL (default: $CAMERA_LAB_LOG_LEVEL or INFO).",
        ),
    ] = None,
    sim: Annotated[
        bool, typer.Option("--sim", help="Add the simulated camera and simulated light.")
    ] = False,
) -> None:
    """Check and tune the robot cell's cameras and lighting."""
    try:
        configure_logging("ERROR" if quiet else log_level)
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--log-level") from exc
    ctx.obj = CliState(sim=sim, quiet=quiet)


# -------------------------------------------------------------------- light
@light_app.command("list")
@handle_errors
def light_list(ctx: typer.Context, json_out: JsonOpt = False) -> None:
    """List light channels with level and limits.

    Example: camera-lab light list --json
    """
    chans = state(ctx).service(ctx).list_lights()
    emit(
        chans,
        json_out,
        lambda: "\n".join(
            f"{c['id']:12s} {c['level_pct']:6.1f} %  "
            f"(max {c['max_pct']} %, step {c['max_step_pct']} %)"
            for c in chans
        ),
    )


@light_app.command("set")
@handle_errors
def light_set(
    ctx: typer.Context,
    levels: Annotated[list[str], typer.Argument(help="channel=percent pairs, e.g. white_ring=40")],
    json_out: JsonOpt = False,
) -> None:
    """Set light levels (ramped and rate-limited). Exit 4 if any channel is rejected.

    Example: camera-lab light set white_ring=40 red=10
    """
    parsed: dict[str, float] = {}
    for item in levels:
        cid, sep, pct = item.partition("=")
        if not sep or not cid:
            raise typer.BadParameter(f"expected channel=percent, got {item!r}")
        try:
            parsed[cid] = float(pct)
        except ValueError:
            raise typer.BadParameter(f"{item!r}: percent must be a number") from None
    res = state(ctx).service(ctx).set_lights(parsed, "human")
    data = {
        "ok": res.ok,
        "levels": res.levels,
        "ramp_steps": res.ramp_steps,
        "results": [r.__dict__ for r in res.results],
    }
    emit(
        data,
        json_out,
        lambda: "\n".join(
            f"{r.status:9s} {r.channel} = {r.requested}" + (f"  ({r.reason})" if r.reason else "")
            for r in res.results
        ),
    )
    if not res.ok:
        raise typer.Exit(EXIT_REJECTED)


@light_app.command("off")
@handle_errors
def light_off(ctx: typer.Context, json_out: JsonOpt = False) -> None:
    """Turn all lights off.

    Example: camera-lab light off
    """
    levels = state(ctx).service(ctx).lights_off("human")
    emit({"levels": levels}, json_out, "all lights off")


# ---------------------------------------------------------------------- mcp
@app.command("mcp")
def mcp(
    ctx: typer.Context,
    sim: Annotated[bool, typer.Option("--sim", help="Add the simulated camera and light.")] = False,
) -> None:
    """Start the MCP server on stdio (the only transport) for Claude Code.

    Configure it in .mcp.json; stdout carries the MCP protocol, logs go to stderr.

    Example: camera-lab mcp --sim
    """
    from camera_lab.core.mcp_server import run_stdio

    run_stdio(sim=state(ctx).sim or sim)


# ------------------------------------------------------------------- manual
@app.command("manual")
@handle_errors
def manual(
    out: Annotated[
        Path | None, typer.Option(help="Write here instead of WS/Manuals/apps/camera_lab.md.")
    ] = None,
) -> None:
    """Write the app page other agents read: what camera_lab does, its revision, and every
    file it reads and writes, from the app's own paths.

    Example: camera-lab manual
    """
    from camera_lab.core.manual import write_manual

    typer.echo(f"wrote {write_manual(out)}")


# ---------------------------------------------------------------------- gui
@app.command("gui")
@handle_errors
def gui(
    ctx: typer.Context,
    sim: Annotated[bool, typer.Option("--sim", help="Add the simulated camera and light.")] = False,
) -> None:
    """Open the desktop GUI (PySide6, dark mode): live view, settings, lights, scores, runs.

    Example: camera-lab gui --sim
    """
    from camera_lab.two_d.gui.app import run_gui  # lazy: only this command needs Qt

    raise typer.Exit(run_gui(sim=state(ctx).sim or sim))


# ------------------------------------------------------------------- limits
@limits_app.command("show")
@handle_errors
def limits_show(ctx: typer.Context, json_out: JsonOpt = False) -> None:
    """Print the effective safety limits (read-only; edit the YAML file and restart to change).

    Example: camera-lab limits show --json
    """
    view = state(ctx).service(ctx).limits_view()
    emit(view, json_out, lambda: to_json(view))


if __name__ == "__main__":  # pragma: no cover
    app()
