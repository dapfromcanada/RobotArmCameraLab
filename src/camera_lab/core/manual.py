"""The app page for other agents: ``WS/Manuals/apps/camera_lab.md`` (Derek 2026-10-27).

What camera_lab does, its revision, and every file and folder it reads and writes, built
from the same path functions the app uses, so the page cannot drift from the code.
Regenerate it with ``camera-lab manual``; never edit the page by hand.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from camera_lab import __version__
from camera_lab.core.paths import (
    APP_ROOT,
    app_config_dir,
    calibration_root,
    camera_backups_root,
    data_root,
    presets_root,
    ws_root,
)

APP_NAME = "camera_lab"


def manual_path() -> Path:
    """``WS/Manuals/apps/camera_lab.md``."""
    return ws_root() / "Manuals" / "apps" / f"{APP_NAME}.md"


@dataclass(frozen=True)
class Place:
    path: Path
    what: str
    kind: str  # "database", "folder", "file"
    kept: str  # how it is kept: git, local only, backups, ...
    truth: bool = False  # the source of truth: never delete or hand-edit


def _places() -> tuple[list[Place], list[Place], list[Place]]:
    """(reads, writes, never touches). Imported late: the modules carry their own paths."""
    from camera_lab.two_d.catalog.papers import papers_out_dir
    from camera_lab.two_d.catalog.store import catalog_backups_dir, catalog_path
    from camera_lab.two_d.fingerprint.common import fingerprints_root
    from camera_lab.two_d.setup import setup_root

    cfg = app_config_dir()
    reads = [
        Place(
            cfg / "safety_limits.yaml", "hard limits for the AI agent (read-only)", "file", "git"
        ),
        Place(cfg / "scoring.yaml", "image score targets", "file", "git"),
        Place(cfg / "lights.yaml", "LED light controllers", "file", "git"),
        Place(cfg / "cameras.yaml", "camera serials and aliases", "file", "git"),
        Place(
            cfg / "camera_profiles",
            "one profile per camera serial: modes, strategy",
            "folder",
            "git",
        ),
        Place(
            cfg / "fingerprint_bench.yaml",
            "the bench: lights, lenses, shortest exposures",
            "file",
            "git",
        ),
        Place(cfg / "charts", "test chart definitions", "folder", "git"),
        Place(
            calibration_root(),
            "camera calibrations (read only, never written)",
            "folder",
            "other app",
        ),
        Place(
            ws_root() / "SDK",
            "vendor camera SDKs (Arena, eBUS), installed into the venv",
            "folder",
            "local only",
        ),
    ]
    writes = [
        Place(
            catalog_path(),
            "SQLite catalog: camera models, cameras, lenses, pairs, fingerprint results, "
            "change log, white-paper text",
            "database",
            "local only; dated copies in the catalog backups",
            truth=True,
        ),
        Place(
            catalog_backups_dir(),
            "dated copies of the catalog after every fingerprint, Add a lens and import",
            "folder",
            "local only (set CAMERA_LAB_CATALOG_BACKUPS for another disk)",
        ),
        Place(
            papers_out_dir(),
            "white papers per camera model and lens, generated from the catalog",
            "folder",
            "git",
        ),
        Place(
            fingerprints_root(),
            "one folder per fingerprint run: frames, fingerprint.yaml, report",
            "folder",
            "local only",
            truth=True,
        ),
        Place(
            camera_backups_root(),
            "camera settings saved before camera_lab changes anything",
            "folder",
            "local only",
            truth=True,
        ),
        Place(setup_root(), "results of the camera setup procedure", "folder", "local only"),
        Place(presets_root(), "saved tuning presets", "folder", "local only"),
        Place(
            data_root(),
            "every capture and tuning run (two_d/<time>_<serial>_<goal>/), site light checks, "
            "gui_messages.log",
            "folder",
            "rsync backup (WS/data is not in git)",
        ),
        Place(manual_path(), "this page", "file", "rsync backup (WS/Manuals is not in git)"),
    ]
    never = [
        Place(ws_root() / "src", "the robot cell's ROS code", "folder", "git"),
        Place(ws_root() / "apps" / "calibration_lab", "the calibration app", "folder", "other app"),
    ]
    return reads, writes, never


def _revision() -> str:
    """Version plus the app's last commit (fixed arguments, no outside input)."""
    git = shutil.which("git")
    if git is None:
        return f"{__version__} (git unknown)"
    try:
        out = subprocess.run(  # noqa: S603 - fixed arguments
            [git, "-C", str(APP_ROOT), "log", "-1", "--format=%h %cs"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        ).stdout.strip()
        dirty = subprocess.run(  # noqa: S603 - fixed arguments
            [git, "-C", str(APP_ROOT), "status", "--porcelain", "--", "src", "config"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return f"{__version__} (git unknown)"
    return f"{__version__}, commit {out}" + (" + uncommitted changes" if dirty else "")


def _size(path: Path) -> str:
    if path == manual_path():
        return "-"
    if not path.exists():
        return "not created yet"
    if path.is_file():
        n = path.stat().st_size
    else:
        n = sum(p.stat().st_size for p in path.rglob("*") if p.is_file())
    for unit in ("B", "kB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024  # type: ignore[assignment]
    return ""


def _rel(path: Path) -> str:
    try:
        return f"WS/{path.relative_to(ws_root())}"
    except ValueError:
        return str(path)


def _table(places: list[Place], sizes: bool) -> list[str]:
    head = "| path | what | kind | kept |" + (" size |" if sizes else "")
    rule = "|---|---|---|---|" + ("---|" if sizes else "")
    out = [head, rule]
    for p in places:
        what = f"**source of truth.** {p.what}" if p.truth else p.what
        row = f"| `{_rel(p.path)}` | {what} | {p.kind} | {p.kept} |"
        out.append(row + (f" {_size(p.path)} |" if sizes else ""))
    return out


def render() -> str:
    reads, writes, never = _places()
    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        f"# {APP_NAME}",
        "",
        f"- **Revision:** {_revision()}",
        f"- **Generated:** {now} by `camera-lab manual`. Never edit this page: regenerate it.",
        f"- **App folder:** `{_rel(APP_ROOT)}` (code, config, spec, tests; README.md and "
        "CONTEXT.md there have the detail)",
        "- **WS** = `" + str(ws_root()) + "`",
        "",
        "## What it does",
        "",
        "A bench tool for humans and AI agents to check, fingerprint and tune the cell's 2D "
        "cameras and LED lights. It measures every camera and camera + lens pair on the bench "
        "(the fingerprint), stores the results in a SQLite catalog, and generates a white paper "
        "per camera model and per lens so on-site agents know how to use each camera. It runs "
        "outside the robot-arm system. 3D and laser parts are planned, not built.",
        "",
        "## How to run it",
        "",
        "| front end | command |",
        "|---|---|",
        "| GUI (humans) | `WS/apps/camera_lab/launch.sh` (`--sim` for a simulated camera) |",
        "| CLI | `WS/environments/camera_lab/bin/camera-lab --help` |",
        "| MCP server (agents) | `camera-lab mcp` (stdio; registered in "
        "`WS/apps/camera_lab/.mcp.json` as `camera-lab`) |",
        "",
        "Agents: start with `get_camera_white_paper`, then `reset_camera`; never trust the "
        "state a camera booted with. Only one program may hold a camera: if the GUI is "
        "connected, ask the human to Disconnect first.",
        "",
        "## Files it writes",
        "",
        "The **source of truth** rows are measured data: never delete or hand-edit them. "
        "Everything else can be regenerated from them.",
        "",
        *_table(writes, sizes=True),
        "",
        "## Files it reads",
        "",
        *_table(reads, sizes=False),
        "",
        "## Never touches",
        "",
        *_table(never, sizes=False),
        "",
        "## Environment variables (override the paths above)",
        "",
        "| variable | changes |",
        "|---|---|",
        "| `CAMERA_LAB_WS_ROOT` | WS itself |",
        "| `CAMERA_LAB_DATA_ROOT` | the run-record folder |",
        "| `CAMERA_LAB_CATALOG` | the catalog database |",
        "| `CAMERA_LAB_CATALOG_BACKUPS` | where catalog copies go (point it at another disk) |",
        "| `CAMERA_LAB_PAPERS_OUT` | where white papers are written |",
        "| `CAMERA_LAB_LOG_LEVEL` | log level |",
        "",
        "## Rules",
        "",
        "- Protected camera features (network, reset, firmware, user-set save) are never "
        "written by agents; the bench fingerprint writes user sets only with the human's "
        "checklist confirmation.",
        "- numpy stays at 1.26.4 (the JAI eBUS SDK needs numpy < 2).",
        "- Owner: Derek. Changes to the app go through `WS/apps/camera_lab/spec/`.",
        "",
    ]
    return "\n".join(lines)


def write_manual(path: Path | None = None) -> Path:
    target = path if path is not None else manual_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render(), encoding="utf-8")
    return target
