"""``camera-lab two-d …``: the 2D command group. No business logic here."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any

import click
import typer

from camera_lab.cli_support import (
    EXIT_REJECTED,
    EXIT_USAGE,
    emit,
    handle_errors,
    parse_floats,
    parse_ints,
    state,
)
from camera_lab.two_d.models import ROI, ApplyResult, CameraSettingsPatch

app = typer.Typer(
    help="2D cameras: discover, settings, capture, score, tune, presets, runs.",
    no_args_is_help=True,
)
settings_app = typer.Typer(help="Read or change camera settings.", no_args_is_help=True)
preset_app = typer.Typer(help="Named presets (settings + light levels).", no_args_is_help=True)
runs_app = typer.Typer(help="Browse recorded runs.", no_args_is_help=True)
profile_app = typer.Typer(help="Camera profiles (characteristics, modes).", no_args_is_help=True)
mode_app = typer.Typer(help="Enter a camera profile mode.", no_args_is_help=True)
catalog_app = typer.Typer(
    help="The camera and lens catalog (SQLite): models, cameras, lenses, runs, changes.",
    no_args_is_help=True,
)
app.add_typer(settings_app, name="settings")
app.add_typer(preset_app, name="preset")
app.add_typer(runs_app, name="runs")
app.add_typer(profile_app, name="profile")
app.add_typer(mode_app, name="mode")
app.add_typer(catalog_app, name="catalog")

JsonOpt = Annotated[
    bool,
    typer.Option(
        "--json",
        help="Machine-readable JSON on stdout (log lines go to stderr; -q silences them).",
    ),
]
CameraArg = Annotated[str, typer.Argument(help="Camera serial or alias (cameras.yaml).")]
OWNER = "cli"


class TuneMethod(StrEnum):
    """Tuning methods available from the CLI."""

    classic = "classic"


class Trigger(StrEnum):
    """Trigger modes."""

    off = "off"
    software = "software"


def _rounded(requested: Any, value: Any) -> bool:
    """A 'clamped' result that only moved to the camera's increment (within 1 %)."""
    try:
        a, b = float(requested), float(value)
    except (TypeError, ValueError):
        return False
    return abs(a - b) <= 0.01 * max(abs(a), 1e-9)


def _apply_text(res: ApplyResult) -> str:
    lines = []
    for r in res.results:
        status: str = r.status
        if status == "clamped" and _rounded(r.requested, r.value):
            status = "rounded"  # to the camera's increment: normal, not a limit
        extra = f" -> {r.value}" if r.status == "clamped" else ""
        why = f"  ({r.reason})" if r.reason and status != "rounded" else ""
        lines.append(f"{status:9s} {r.field} = {r.requested}{extra}{why}")
    lines.extend(f"warning: {w}" for w in res.warnings)
    return "\n".join(lines) or "nothing changed"


# ----------------------------------------------------------------- discover
@app.command("discover")
@handle_errors
def discover(
    ctx: typer.Context,
    sim: Annotated[bool, typer.Option("--sim", help="Also list the simulated camera.")] = False,
    json_out: JsonOpt = False,
) -> None:
    """List connected 2D cameras (serial, alias, vendor, model, version, interface, IP).

    "version" is what discovery reports: the firmware on Lucid cameras, the device version
    on the JAI. The firmware proper (DeviceFirmwareVersion) is read when a camera is opened.

    Example: camera-lab two-d discover --sim --json
    """
    st = state(ctx)
    st.sim = st.sim or sim
    svc = st.service(ctx)
    cams = svc.discover()
    names: dict[str, list[str]] = {}
    for alias, serial in svc.cameras.aliases.items():
        names.setdefault(serial, []).append(alias)
    emit(
        [{**c.model_dump(mode="json"), "aliases": names.get(c.serial, [])} for c in cams],
        json_out,
        lambda: (
            "\n".join(
                f"{c.serial:12s} {','.join(names.get(c.serial, [])) or '-':10s} {c.vendor:20s} "
                f"{c.model:16s} version {c.firmware or '-':12s} {c.interface:10s} "
                f"{c.ip or '-':16s} {c.driver}"
                for c in cams
            )
            or "no cameras found"
        ),
    )


# ----------------------------------------------------------------- settings
@settings_app.command("get")
@handle_errors
def settings_get(ctx: typer.Context, camera: CameraArg, json_out: JsonOpt = False) -> None:
    """Show current settings and effective allowed ranges.

    Example: camera-lab --sim two-d settings get SIM-0001
    """
    svc = state(ctx).service(ctx)
    svc.open(camera, OWNER)
    view = svc.get_settings(camera).to_dict()
    view["mode"] = svc.current_mode(camera)  # entered, or recognised from the settings

    def human() -> str:
        lines = [f"{'mode':14s} {view['mode'] or '(no profile mode matches)'}"]
        lines += [f"{k:14s} {v}" for k, v in view["settings"].items()]
        lines.append("effective ranges:")
        for name, r in view["ranges"].items():
            if r["adjustable"]:
                lines.append(f"  {name:14s} {r['min']} .. {r['max']} (step {r['increment']})")
            else:
                lines.append(f"  {name:14s} not adjustable: {r['reason']}")
        lines.extend(f"  {k:14s} {', '.join(v)}" for k, v in view["options"].items())
        return "\n".join(lines)

    emit(view, json_out, human)


@settings_app.command("set")
@handle_errors
def settings_set(
    ctx: typer.Context,
    camera: CameraArg,
    exposure_us: Annotated[
        float | None, typer.Option("--exposure-us", help="Exposure, µs.")
    ] = None,
    gain_db: Annotated[float | None, typer.Option("--gain-db", help="Gain, dB.")] = None,
    gamma: Annotated[float | None, typer.Option("--gamma")] = None,
    wb: Annotated[str | None, typer.Option("--wb", help="White balance R,G,B ratios.")] = None,
    pixel_format: Annotated[str | None, typer.Option("--pixel-format")] = None,
    roi: Annotated[str | None, typer.Option("--roi", help="X,Y,W,H in pixels.")] = None,
    fps: Annotated[float | None, typer.Option("--fps", help="Frame rate, Hz.")] = None,
    trigger: Annotated[Trigger | None, typer.Option("--trigger")] = None,
    adv: Annotated[
        list[str] | None,
        typer.Option(
            "--adv",
            help="Advanced setting KEY=VALUE (repeatable), e.g. --adv black.level=1 "
            "--adv hdr.output=HDR; the keys are listed by `settings get`.",
        ),
    ] = None,
    json_out: JsonOpt = False,
) -> None:
    """Apply validated changes; prints applied / clamped / rejected per field.

    Exit code 4 if any field was rejected. Settings that change as a side effect (e.g. the
    exposure shortened to fit a frame rate) are reported as warnings. The camera is backed
    up before the first change of each command (config/camera_backups/<serial>/).

    Example: camera-lab --sim two-d settings set SIM-0001 --exposure-us 20000 --gain-db 3
    """
    fields: dict[str, Any] = {
        "exposure_us": exposure_us,
        "gain_db": gain_db,
        "gamma": gamma,
        "pixel_format": pixel_format,
        "frame_rate_hz": fps,
        "trigger_mode": trigger.value if trigger else None,
    }
    if wb is not None:
        r, g, b = parse_floats(wb, 3, "--wb")
        fields["white_balance"] = {"red": r, "green": g, "blue": b}
    if roi is not None:
        x, y, w, h = parse_ints(roi, 4, "--roi")
        fields["roi"] = ROI(x=x, y=y, width=w, height=h)
    if adv:
        advanced: dict[str, Any] = {}
        for item in adv:
            key, sep, raw = item.partition("=")
            if not sep or not key.strip():
                raise typer.BadParameter(f"--adv needs KEY=VALUE, got {item!r}")
            try:
                advanced[key.strip()] = float(raw)
            except ValueError:
                advanced[key.strip()] = raw.strip()
        fields["advanced"] = advanced
    patch = CameraSettingsPatch(**{k: v for k, v in fields.items() if v is not None})
    if not patch.changed_fields():
        raise typer.BadParameter("nothing to set; give at least one setting option")
    svc = state(ctx).service(ctx)
    svc.open(camera, OWNER)
    res = svc.apply(camera, patch, "human")
    emit(res.model_dump(mode="json"), json_out, lambda: _apply_text(res))
    if not res.ok:
        raise typer.Exit(EXIT_REJECTED)


# ------------------------------------------------------------------ capture
@app.command("capture")
@handle_errors
def capture(
    ctx: typer.Context,
    camera: CameraArg,
    count: Annotated[int, typer.Option("--count", min=1, help="Number of frames.")] = 1,
    run: Annotated[str | None, typer.Option("--run", help="Add to this run id.")] = None,
    note: Annotated[str, typer.Option("--note", help="Free text (≤ 500 chars).")] = "",
    json_out: JsonOpt = False,
) -> None:
    """Capture, score and save frames. A new run is created when --run is omitted.

    Example: camera-lab --sim two-d capture SIM-0001 --count 3 --note "bin, ring light"
    """
    svc = state(ctx).service(ctx)
    svc.open(camera, OWNER)
    results = svc.capture(camera, count, run, note, "human")
    emit(
        [r.to_dict() for r in results],
        json_out,
        lambda: "\n".join(
            f"{r.capture_id}  composite {r.scores.composite:.2f}  "
            f"mean {r.scores.metric('mean_level'):.3f}  "
            f"clipped {r.scores.metric('clipped_pct'):.2f}%"
            + (
                "  charts "
                + ", ".join(
                    str(c.get("chart_id") or f"unknown tag {c['tag_ids']}") for c in r.charts
                )
                if r.charts
                else ""
            )
            for r in results
        ),
    )


# -------------------------------------------------------------------- score
@app.command("score")
@handle_errors
def score(
    ctx: typer.Context,
    image: Annotated[Path, typer.Argument(help="A .tif/.tiff file.")],
    json_out: JsonOpt = False,
) -> None:
    """Score an existing TIFF (deterministic).

    Example: camera-lab two-d score captures/0001.tiff --json
    """
    report = state(ctx).service(ctx).score_file(image)
    emit(
        report.model_dump(mode="json"),
        json_out,
        lambda: "\n".join(
            [
                f"{m.name:12s} {m.value}"
                + ("" if m.passed is None else f"  {'ok' if m.passed else 'FAIL'}")
                for m in report.metrics
            ]
            + [f"{'composite':12s} {report.composite}"]
        ),
    )


# --------------------------------------------------------------------- tune
@app.command("tune")
@handle_errors
def tune(
    ctx: typer.Context,
    camera: CameraArg,
    method: Annotated[
        TuneMethod,
        typer.Option("--method", help="classic = non-AI tuner (agent tuning is via MCP)."),
    ] = TuneMethod.classic,
    mode: Annotated[
        str | None,
        typer.Option("--mode", help="Profile mode (default: the profile's default mode)."),
    ] = None,
    strategy: Annotated[
        str | None,
        typer.Option("--strategy", help="Override the mode's strategy (linear, prism, hdr)."),
    ] = None,
    target_mean: Annotated[float | None, typer.Option("--target-mean", min=0.1, max=0.9)] = None,
    max_steps: Annotated[int, typer.Option("--max-steps", min=1)] = 15,
    wb: Annotated[bool, typer.Option("--wb", help="Also grey-world white balance.")] = False,
    option: Annotated[
        list[str] | None,
        typer.Option("--option", help="Strategy option KEY=VALUE, e.g. wb_method=exposure."),
    ] = None,
    json_out: JsonOpt = False,
) -> None:
    """Enter a profile mode and run its non-AI tuning strategy; the run is saved.

    Example: camera-lab --sim two-d tune SIM-0001 --method classic --mode hdr
    """
    svc = state(ctx).service(ctx)
    svc.open(camera, OWNER)
    options: dict[str, str] = {}
    for item in option or []:
        key, sep, value = item.partition("=")
        if not sep or not key:
            raise typer.BadParameter(f"expected KEY=VALUE, got {item!r}", param_hint="--option")
        options[key] = value
    result = svc.tune(
        camera,
        mode,
        strategy,
        max_steps=max_steps,
        target_mean=target_mean,
        white_balance=wb,
        options=options,
    )
    emit(
        result.to_dict(),
        json_out,
        lambda: (
            f"[{result.mode}/{result.strategy}] {result.message}: run {result.run_id}, "
            f"{result.steps_used} steps, best {result.best_capture_id} "
            f"(composite {result.best_composite})"
        ),
    )


# ---------------------------------------------------------- profiles/modes
@profile_app.command("show")
@handle_errors
def profile_show(ctx: typer.Context, camera: CameraArg, json_out: JsonOpt = False) -> None:
    """Show a camera's profile (characteristics, modes, strategies, knobs).

    Example: camera-lab two-d profile show triton
    """
    prof = state(ctx).service(ctx).get_profile(camera)
    data = prof.model_dump(mode="json", exclude_none=True)

    def human() -> str:
        c = prof.characteristics
        lines = [
            f"{prof.serial}  {prof.label}",
            f"  shutter {c.shutter}, colour {c.colour}, HDR {'yes' if c.hdr_capable else 'no'}, "
            f"black level {c.black_level if c.black_level is not None else 'unmeasured'}",
        ]
        for name, m in prof.modes.items():
            star = "*" if name == prof.default_mode else " "
            lines.append(f" {star}{name:10s} strategy {m.strategy:7s} knobs {', '.join(m.knobs)}")
        return "\n".join(lines)

    emit(data, json_out, human)


@mode_app.command("set")
@handle_errors
def mode_set(ctx: typer.Context, camera: CameraArg, mode: str, json_out: JsonOpt = False) -> None:
    """Enter a profile mode (applies its settings through the safety limits).

    Example: camera-lab two-d mode set triton linear
    """
    svc = state(ctx).service(ctx)
    svc.open(camera, OWNER)
    res = svc.set_mode(camera, mode, "human")
    emit(res.model_dump(mode="json"), json_out, lambda: f"mode {mode}\n{_apply_text(res)}")


# ------------------------------------------------------------------ presets
@preset_app.command("list")
@handle_errors
def preset_list(
    ctx: typer.Context,
    camera: Annotated[str | None, typer.Argument(help="Only this camera.")] = None,
    json_out: JsonOpt = False,
) -> None:
    """List presets.

    Example: camera-lab two-d preset list U330439
    """
    refs = state(ctx).service(ctx).list_presets(camera)
    emit(
        [{"serial": r.serial, "name": r.name, "path": str(r.path)} for r in refs],
        json_out,
        lambda: "\n".join(f"{r.serial:12s} {r.name}" for r in refs) or "no presets",
    )


@preset_app.command("save")
@handle_errors
def preset_save(
    ctx: typer.Context,
    camera: CameraArg,
    name: Annotated[str, typer.Argument(help="Preset name [a-z0-9_-]{1,40}.")],
    from_capture: Annotated[str, typer.Option("--from-capture", help="<run_id>/<NNNN>")],
    force: Annotated[
        bool, typer.Option("--force", help="Overwrite (old file kept as .bak).")
    ] = False,
    json_out: JsonOpt = False,
) -> None:
    """Save a capture's settings and light levels as a preset.

    Example:
      camera-lab two-d preset save SIM-0001 bin_ring \\
        --from-capture 20261021-101500_SIM-0001_run/0001
    """
    path = state(ctx).service(ctx).save_preset(camera, name, from_capture, overwrite=force)
    emit({"path": str(path)}, json_out, f"saved {path}")


@preset_app.command("apply")
@handle_errors
def preset_apply(
    ctx: typer.Context, camera: CameraArg, name: str, json_out: JsonOpt = False
) -> None:
    """Apply a preset (settings through the safety limits, then lights). Exit 4 on rejections.

    Example: camera-lab --sim two-d preset apply SIM-0001 bin_ring
    """
    svc = state(ctx).service(ctx)
    svc.open(camera, OWNER)
    res = svc.apply_preset(camera, name, "human")
    lights_ok = res.lights is None or res.lights.ok
    data = {
        "settings": res.apply.model_dump(mode="json"),
        "lights": None if res.lights is None else res.lights.levels,
        "ok": res.apply.ok and lights_ok,
    }
    emit(data, json_out, lambda: _apply_text(res.apply))
    if not (res.apply.ok and lights_ok):
        raise typer.Exit(EXIT_REJECTED)


@preset_app.command("show")
@handle_errors
def preset_show(
    ctx: typer.Context, camera: CameraArg, name: str, json_out: JsonOpt = False
) -> None:
    """Show a preset.

    Example: camera-lab two-d preset show U330439 bin_ring
    """
    p = state(ctx).service(ctx).get_preset(camera, name)
    emit(p.model_dump(mode="json"), json_out)


# --------------------------------------------------------------------- runs
@runs_app.command("list")
@handle_errors
def runs_list(
    ctx: typer.Context,
    camera: Annotated[str | None, typer.Option("--camera", help="Only this camera.")] = None,
    json_out: JsonOpt = False,
) -> None:
    """List runs, newest first.

    Example: camera-lab two-d runs list --camera SIM-0001
    """
    runs = state(ctx).service(ctx).list_runs(camera)
    emit(
        [r.__dict__ for r in runs],
        json_out,
        lambda: (
            "\n".join(
                f"{r.run_id}  {r.method:8s} {r.capture_count:4d} captures  best {r.best_score}"
                for r in runs
            )
            or "no runs"
        ),
    )


@runs_app.command("show")
@handle_errors
def runs_show(ctx: typer.Context, run_id: str, json_out: JsonOpt = False) -> None:
    """Show a run's run.json.

    Example: camera-lab two-d runs show 20261021-101500_SIM-0001_run
    """
    rec = state(ctx).service(ctx).get_run(run_id)
    emit(rec.model_dump(mode="json"), json_out)


# --------------------------------------------------------- backup / restore
@app.command("backup")
@handle_errors
def backup(
    ctx: typer.Context,
    camera: CameraArg,
    note: Annotated[str, typer.Option("--note", help="Why (≤ 300 chars).")] = "manual backup",
    json_out: JsonOpt = False,
) -> None:
    """Save the camera's whole configuration to WS/config/camera_backups/<serial>/.

    Example: camera-lab two-d backup triton --note "before bench setup"
    """
    svc = state(ctx).service(ctx)
    svc.open(camera, OWNER)
    b, created = svc.backup(camera, note)
    data = {"backup_id": b.backup_id, "serial": b.serial, "created": created, "reason": b.reason}
    emit(
        data,
        json_out,
        f"{'saved' if created else 'unchanged since'} backup {b.backup_id} ({b.serial})",
    )


@app.command("backups")
@handle_errors
def backups(ctx: typer.Context, camera: CameraArg, json_out: JsonOpt = False) -> None:
    """List a camera's settings backups, oldest first.

    Example: camera-lab two-d backups triton
    """
    items = state(ctx).service(ctx).list_backups(camera)
    data = [
        {"backup_id": b.backup_id, "created_utc": b.created_utc.isoformat(), "reason": b.reason}
        for b in items
    ]
    emit(
        data,
        json_out,
        lambda: "\n".join(f"{d['backup_id']}  {d['reason']}" for d in data) or "no backups",
    )


@app.command("restore")
@handle_errors
def restore(
    ctx: typer.Context,
    camera: CameraArg,
    backup_id: Annotated[str, typer.Argument(help="Backup id (see: two-d backups).")],
    yes: Annotated[bool, typer.Option("--yes", help="Do not ask for confirmation.")] = False,
    json_out: JsonOpt = False,
) -> None:
    """Write a backup back to the camera (asks first; the current state is backed up).

    Example: camera-lab two-d restore triton 20261021T093500Z
    """
    if not yes and not typer.confirm(
        f"Restore backup {backup_id} to camera {camera}? Its current settings are backed up first."
    ):
        raise typer.Exit(EXIT_USAGE)
    svc = state(ctx).service(ctx)
    svc.open(camera, OWNER)
    report, before = svc.restore_backup(camera, backup_id, "human")
    data = {**report.model_dump(mode="json"), "backup_before_restore": before, "ok": report.ok}

    def human() -> str:
        lines = [
            f"restored {backup_id}: {report.written} written, {report.unchanged} unchanged, "
            f"{report.informational} read-only (not restored); "
            f"previous state saved as {before}"
        ]
        lines += [f"  failed {f['feature']}: {f['reason']}" for f in report.failed]
        lines += [f"  differs {d}" for d in report.differences]
        lines += [f"  protected, not restored: {d}" for d in report.protected_skipped]
        return "\n".join(lines)

    emit(data, json_out, human)
    if not report.ok:
        raise typer.Exit(EXIT_REJECTED)


# ------------------------------------------------------------------- setup
@app.command("setup")
@handle_errors
def setup(
    ctx: typer.Context,
    camera: CameraArg,
    focus_locked: Annotated[
        bool, typer.Option("--focus-locked", help="Focus is already set (no live focus loop).")
    ] = False,
    yes_save: Annotated[
        bool, typer.Option("--yes-save", help="Save to User Set 1 without asking.")
    ] = False,
    preset: Annotated[str | None, typer.Option("--preset", help="Preset name to save.")] = None,
) -> None:
    """Guided camera setup: backup, light, brightness, focus, WB, save to User Set 1.

    Every step passes or stops and says what it needs. During focus, turn the
    focus ring while the live MTF50 (resolution chart) is printed; press Enter
    when the ring is set and locked. Saving writes camera memory and asks first.

    Example: camera-lab two-d setup triton
    """
    import select
    import sys

    from camera_lab.two_d.setup import SetupSession

    svc = state(ctx).service(ctx)
    svc.open(camera, OWNER)
    ses = SetupSession(svc, camera, actor="human")

    def show(res: Any) -> None:
        mark = {"pass": "ok  ", "skipped": "skip", "fail": "STOP"}[res.status]
        typer.echo(f"[{mark}] {res.step}: {res.message}")
        if res.status == "fail":
            report = ses.write_report()
            if report is not None:
                typer.echo(f"report: {report}")
            raise typer.Exit(EXIT_REJECTED)

    show(ses.step_backup())
    show(ses.step_light())
    show(ses.step_brightness())
    locked = focus_locked
    if not focus_locked:
        if not sys.stdin.isatty():
            raise typer.BadParameter("focus needs a terminal (or --focus-locked)")
        typer.echo("focus: turn the focus ring for the highest MTF50; press Enter when locked")
        while True:
            r = ses.focus_reading()
            value = r["mtf50_cyc_px"]
            line = (
                f"  MTF50 {value:.3f} cycles/px  (best {r['best']:.3f})"
                if value is not None
                else f"  {r.get('message', 'no reading')}"
            )
            typer.echo(line)
            if select.select([sys.stdin], [], [], 1.0)[0]:
                sys.stdin.readline()
                locked = True
                break
    show(ses.finish_focus(locked))
    show(ses.step_brightness_recheck())
    show(ses.step_white_balance())
    confirm = yes_save or typer.confirm("Save these settings to the camera's User Set 1?")
    on_site = confirm and typer.confirm(
        "Is this camera installed on site (make User Set 1 its boot set)? In the lab it keeps "
        "booting from factory Default",
        default=False,
    )
    res = ses.save(confirm, preset, on_site=on_site)
    show(res)
    for note in res.data.get("notes", []):
        typer.echo(f"note: {note}")


# ---------------------------------------------------------------- boot set
@app.command("boot-set")
@handle_errors
def boot_set_cmd(
    ctx: typer.Context,
    camera: CameraArg,
    slot: str = typer.Option("Default", help="user set to boot from (Default = factory)"),
    yes: bool = typer.Option(False, "--yes", help="confirm: this writes camera memory"),
) -> None:
    """Choose the user set the camera boots from (writes camera memory; needs --yes).

    Default (factory) is recommended: anything that opens the camera then starts with gain
    0 and no one's old settings. camera_lab tools still reset to the design mode.

    Example: camera-lab two-d boot-set triton --slot Default --yes
    """
    if not yes:
        raise typer.BadParameter("this writes camera memory: add --yes to confirm")
    svc = state(ctx).service(ctx)
    svc.open(camera, OWNER)
    out = svc.set_boot_user_set(camera, slot, confirm=True, actor="human")
    typer.echo(f"boot user set: {out['was']} -> {out['boot_set']} (backup {out['backup_before']})")


@app.command("reboot")
@handle_errors
def reboot_cmd(
    ctx: typer.Context,
    camera: CameraArg,
    yes: bool = typer.Option(False, "--yes", help="confirm the camera restart"),
) -> None:
    """Restart the camera (DeviceReset): like a power cycle, it reloads its boot user set.

    Waits for the camera to come back (≈ 20–30 s), reopens it and checks the restart by its
    uptime. The camera is backed up first. Needs --yes.

    Example: camera-lab two-d reboot triton --yes
    """
    if not yes:
        raise typer.BadParameter("this restarts the camera: add --yes to confirm")
    svc = state(ctx).service(ctx)
    svc.open(camera, OWNER)
    out = svc.reboot_camera(camera, confirm=True, actor="human")
    up = out["uptime_after_s"]
    typer.echo(
        f"camera back; uptime {up:.0f} s" if up is not None else "camera back (no uptime counter)"
    )
    typer.echo(f"restart verified: {out['restart_verified']} ({out['verified_by']})")


# ------------------------------------------------------------------- reset
@app.command("reset")
@handle_errors
def reset_cmd(
    ctx: typer.Context,
    camera: CameraArg,
    mode: str = typer.Option("", help="profile mode to enter (default: the design mode)"),
) -> None:
    """Start from a known state: factory Default, then the profile mode, every setting read back.

    Never trust what the camera booted with (a user set may hold someone's old settings).
    Live only: nothing is written to the camera's memory; the camera is backed up first.

    Example: camera-lab two-d reset triton
    """
    svc = state(ctx).service(ctx)
    svc.open(camera, OWNER)
    out = svc.reset_camera(camera, mode or None, actor="human")
    typer.echo(
        f"loaded {out['loaded']}, mode {out['mode']}: " + ("ok" if out["ok"] else "MISMATCH")
    )
    for m in out["mismatches"]:
        typer.echo(f"  ! {m}")
    s = out["settings"]
    typer.echo(
        f"  {s['pixel_format']}, exposure {s['exposure_us']:.0f} µs, gain {s['gain_db']:.1f} dB, "
        f"white balance {s.get('white_balance')}"
    )
    typer.echo(f"  backup before: {out['backup_before']}")


@app.command("verify")
@handle_errors
def verify_cmd(ctx: typer.Context, camera: CameraArg) -> None:
    """Verify the camera's state on whatever it sees (the white paper's checks).

    Design-mode settings in place, gain as in the baseline, linear output (× 2.00 per
    exposure doubling), no dark pixel clipped at 0, finest shadow step 1 DN. Changes
    nothing (the exposure is put back after the linearity test).

    Example: camera-lab two-d verify triton
    """
    from camera_lab.two_d.site.verify import verify_camera

    svc = state(ctx).service(ctx)
    svc.open(camera, OWNER)
    checks = verify_camera(svc, camera, actor="human")
    marks = {"pass": "ok  ", "warn": "WARN", "fail": "FAIL", "info": "info"}
    for c in checks:
        typer.echo(f"[{marks[c.status]}] {c.name}: {c.message}")
        if c.action:
            typer.echo(f"         → {c.action}")


# -------------------------------------------------------------- site light
@app.command("assess-light")
@handle_errors
def assess_light_cmd(
    ctx: typer.Context,
    camera: CameraArg,
    max_exposure_ms: float = typer.Option(
        0.0, help="exposure budget in ms (default: one frame at the camera's frame rate)"
    ),
    apply: bool = typer.Option(False, help="leave the camera at the recommended exposure"),
    reset: bool = typer.Option(
        True, help="start from factory Default + the design mode (--no-reset: as it is)"
    ),
) -> None:
    """Assess the light on the scene the camera sees, against the camera model's baseline.

    No test jig: point the camera at the real scene. Checks the camera state (linear
    output), flicker, brightness, SNR, clipping, scene structure and the light distribution,
    and says what to change. Report saved under the data folder (two_d/site_light/<serial>/).

    The camera is backed up, reset to factory Default + its design mode, measured, and then
    PUT BACK as it was — unless --apply, which leaves it in the design mode at the
    recommended exposure.

    Example: camera-lab two-d assess-light triton
    """
    from camera_lab.two_d.site.light import assess_light

    svc = state(ctx).service(ctx)
    svc.open(camera, OWNER)
    rep = assess_light(
        svc,
        camera,
        actor="human",
        max_exposure_us=max_exposure_ms * 1000 or None,
        apply=apply,
        reset=reset,
    )
    marks = {"pass": "ok  ", "warn": "WARN", "fail": "FAIL", "info": "info"}
    for c in rep.checks:
        typer.echo(f"[{marks[c.status]}] {c.name}: {c.message}")
        if c.action:
            typer.echo(f"         → {c.action}")
    typer.echo(f"\n{rep.summary()}")
    if rep.folder:
        typer.echo(f"report: {rep.folder}")


# ------------------------------------------------------------- fingerprint
@app.command("fingerprint")
@handle_errors
def fingerprint(ctx: typer.Context, camera: CameraArg) -> None:
    """Measure the camera's fingerprint (Phase 11): timing, darks, flat, HDR channels, noise.

    Each step says what to set (lens cap, diffuser, dimmer, iris) and asks you to
    confirm every item; camera_lab measures the light where a recipe dictates it.
    The camera is backed up first and restored at the end.

    Example: camera-lab two-d fingerprint triton
    """
    from camera_lab.two_d.fingerprint.session import FingerprintSession

    svc = state(ctx).service(ctx)
    svc.open(camera, OWNER)
    ses = FingerprintSession(svc, camera, actor="human")

    def show(res: Any) -> bool:
        mark = {"pass": "ok  ", "skipped": "skip", "fail": "STOP"}[res.status]
        typer.echo(f"[{mark}] {ses.plan[res.step].label}: {res.message}")
        return bool(res.ok)

    def pick(title: str, options: list[str]) -> int:
        """A numbered pick (nothing is typed but the number)."""
        typer.echo(title)
        for i, text in enumerate(options, 1):
            typer.echo(f"  {i}. {text}")
        return int(typer.prompt("  Number", type=click.IntRange(1, len(options)))) - 1

    if not ses.lens_choices:
        raise typer.BadParameter(
            "the catalog has no lens for this camera: add it first (GUI: Add a lens)"
        )
    if ses.lens_id is None:
        choices = ses.lens_choices
        n = pick(
            "Which lens is on the camera?",
            [
                spec.label + (" (tested on this model)" if tried else "")
                for _, spec, tried in choices
            ],
        )
        ses.select_lens(choices[n][0])
    typer.echo(
        f"Lens: {ses.lens_label}"
        + (" (light recipe from the camera profile)" if ses.recipe else "")
    )
    while (step := ses.next_step()) is not None:
        plan = ses.plan[step]
        typer.echo(f"\n{plan.label}\n  {plan.instruction}")
        if plan.iris_pick and ses.lens_spec is not None:
            stops = list(ses.lens_spec.f_stops)
            ses.set_iris(stops[pick("Which stop is the iris set to?", [f"f/{f:g}" for f in stops])])
        for check in plan.checks:  # every item must be confirmed (strict procedure)
            while not typer.confirm(f"  {check}?", default=False):
                typer.echo("  Do it first, then answer yes (Ctrl-C stops the fingerprint).")
        failed = not show(ses.run(step, plan.checks))
        if (
            failed
            and plan.can_skip
            and typer.confirm("  Skip this setup (recorded in the report)?", default=False)
        ):
            show(ses.skip(step, typer.prompt("  Why", default="")))


# ------------------------------------------------------------------ catalog
@catalog_app.command("import")
@handle_errors
def catalog_import_cmd(
    papers: Annotated[
        bool,
        typer.Option(
            "--papers",
            help="Also load the hand-written papers (config/camera_models/<model>.md / .yaml) "
            "as sections and sheet cells: once, before they are retired (it overwrites edits).",
        ),
    ] = False,
    json_out: JsonOpt = False,
) -> None:
    """Load the bench file's lenses, the camera profiles and every fingerprint folder into the
    catalog. Safe to repeat: rows are updated in place (each change logged).

    Example: camera-lab two-d catalog import
    """
    from camera_lab.two_d.catalog import Catalog
    from camera_lab.two_d.catalog.importer import import_all
    from camera_lab.two_d.catalog.papers import import_papers

    with Catalog() as cat:
        rep = import_all(cat)
        paper_facts = import_papers(cat) if papers else {}
        path = cat.path
    data = {
        **rep.__dict__,
        "notes": list(rep.notes),
        "catalog": str(path),
        "papers": paper_facts,
    }
    emit(
        data,
        json_out,
        lambda: (
            f"{path}: {rep.camera_models} camera models, {rep.cameras} cameras, {rep.lenses} "
            f"lenses, {rep.runs} runs, {rep.results} results, {rep.pair_stops} per-stop values"
            + "".join(f"\n  note: {n}" for n in rep.notes)
        ),
    )


@catalog_app.command("lenses")
@handle_errors
def catalog_lenses_cmd(
    model: Annotated[
        str | None, typer.Option(help="Camera model: lenses tested on it first.")
    ] = None,
    json_out: JsonOpt = False,
) -> None:
    """The lenses in the catalog with their marked stops and measured light per stop.

    Example: camera-lab two-d catalog lenses --model AP-3200T-PGE
    """
    from camera_lab.two_d.catalog import Catalog

    with Catalog() as cat:
        lenses = cat.lenses_for_model(model) if model else cat.lenses()
    data = [
        {
            "id": x.id,
            "name": x.name,
            "focal_mm": x.focal_mm,
            "mount": x.mount,
            "stops": [
                {"f": s.f_number, "end_stop": s.end_stop, "transmission": s.transmission}
                for s in x.stops
            ],
            "tested_on": list(x.tested_on),
        }
        for x in lenses
    ]

    def human() -> str:
        lines = []
        for x in lenses:
            stops = "  ".join(
                f"f/{s.f_number:g}"
                + (f" ({s.end_stop})" if s.end_stop else "")
                + (f"={s.transmission:.3g}" if s.transmission is not None else "")
                for s in x.stops
            )
            tested = ", ".join(x.tested_on) or "no camera yet"
            lines.append(
                f"{x.id}: {x.name}, {x.focal_mm:g} mm {x.mount or ''} — tested on {tested}"
            )
            lines.append(f"    {stops}")
        return "\n".join(lines) or "no lenses"

    emit(data, json_out, human)


@catalog_app.command("runs")
@handle_errors
def catalog_runs_cmd(
    camera: Annotated[str | None, typer.Argument(help="Camera serial (all if omitted).")] = None,
    json_out: JsonOpt = False,
) -> None:
    """Fingerprint runs and live checks in the catalog.

    Example: camera-lab two-d catalog runs U330439
    """
    from camera_lab.two_d.catalog import Catalog

    with Catalog() as cat:
        runs = cat.runs(camera)

    def human() -> str:
        return (
            "\n".join(
                f"{r['id']}  {r['kind']}  lens {r['lens_id'] or '-'}"
                + (f" f/{r['f_number']:g}" if r["f_number"] else "")
                + ("" if r["valid"] else "  [history only]")
                + (f"  — {r['note']}" if r["note"] else "")
                for r in runs
            )
            or "no runs"
        )

    emit(runs, json_out, human)


@catalog_app.command("changes")
@handle_errors
def catalog_changes_cmd(
    limit: Annotated[int, typer.Option(min=1, max=10_000)] = 30, json_out: JsonOpt = False
) -> None:
    """The latest catalog changes: who, when, what, old → new.

    Example: camera-lab two-d catalog changes --limit 50
    """
    from camera_lab.two_d.catalog import Catalog

    with Catalog() as cat:
        rows = cat.changes(limit)

    def human() -> str:
        return (
            "\n".join(
                f"{c['utc']}  {c['actor']:<11} {c['tbl']}/{c['row_key']}"
                + (f".{c['col']}: {c['old']} → {c['new']}" if c["col"] else f": {c['new']}")
                for c in rows
            )
            or "no changes"
        )

    emit(rows, json_out, human)


@catalog_app.command("papers")
@handle_errors
def catalog_papers_cmd(
    model: Annotated[str | None, typer.Option(help="One camera model (all if omitted).")] = None,
    json_out: JsonOpt = False,
) -> None:
    """Generate the white papers from the catalog: config/camera_models/<model>/camera.md +
    camera.yaml and lenses/<lens>.md + .yaml per lens tested on the model.

    Example: camera-lab two-d catalog papers --model AP-3200T-PGE
    """
    from camera_lab.two_d.catalog import Catalog
    from camera_lab.two_d.catalog.papers import write_papers

    with Catalog() as cat:
        models = [model] if model else [m["id"] for m in cat.camera_models()]
        written = {m: [str(p) for p in write_papers(cat, m)] for m in models}
    emit(
        written,
        json_out,
        lambda: "\n".join(
            (f"{m}: {len(fs)} files in {Path(fs[0]).parent}" if fs else f"{m}: nothing yet")
            for m, fs in written.items()
        ),
    )


@catalog_app.command("facts")
@handle_errors
def catalog_facts_cmd(
    model: Annotated[str, typer.Argument(help="Camera model, e.g. AP-3200T-PGE.")],
    set_: Annotated[
        list[str] | None,
        typer.Option(
            "--set",
            help="KEY=JSON, e.g. performance/black_dn=100 (one cell of the sheet; logged).",
        ),
    ] = None,
    prefix: Annotated[str, typer.Option(help="Only cells under this path.")] = "",
    json_out: JsonOpt = False,
) -> None:
    """The model's sheet as cells (key = path in the sheet); --set changes one cell, logged
    in the change log. Regenerate the papers afterwards (catalog papers).

    Example: camera-lab two-d catalog facts AP-3200T-PGE --prefix performance
    """
    import json as _json

    from camera_lab.two_d.catalog import Catalog

    with Catalog() as cat:
        for item in set_ or ():
            key, sep, raw = item.partition("=")
            if not sep:
                raise typer.BadParameter(f"--set needs KEY=JSON, got {item!r}")
            try:
                value = _json.loads(raw)
            except ValueError:
                value = raw  # plain text
            cat.set_fact(model, key.strip(), value, source="manual", actor="human")
        facts = {k: v for k, v in cat.facts(model).items() if k.startswith(prefix)}
    emit(facts, json_out, lambda: "\n".join(f"{k} = {_json.dumps(v)}" for k, v in facts.items()))
