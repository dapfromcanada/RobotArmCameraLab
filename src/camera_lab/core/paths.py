"""Filesystem roots and the single safe path builder for camera_lab.

SECURITY-SENSITIVE. Every path built from user, agent, camera or YAML input must
go through :func:`safe_join`. Nothing else in camera_lab may concatenate such
input into a path.

Roots (functions, so tests can override them through the environment):

- :func:`ws_root`: ``CAMERA_LAB_WS_ROOT`` or ``/mnt/workspaces/robotic_cell``
- :func:`data_root`: ``CAMERA_LAB_DATA_ROOT`` or ``<WS>/data/raw_recordings/camera_lab``
- :func:`presets_root`: ``<WS>/config/camera_presets``
- :func:`calibration_root`: ``<WS>/calibration`` (read-only for this app)
"""

from __future__ import annotations

import os
import re
from pathlib import Path

DEFAULT_WS_ROOT = Path("/mnt/workspaces/robotic_cell")

#: ``APP/``: the camera_lab project folder (holds ``config/``, ``spec/``, ...).
APP_ROOT = Path(__file__).resolve().parents[3]

#: User/agent slug (run slug, from a note or goal): lower-case, digits, ``-``.
SLUG_RE = re.compile(r"[a-z0-9-]{1,40}")
#: Preset name: like a slug, but ``_`` is also allowed.
PRESET_NAME_RE = re.compile(r"[a-z0-9_-]{1,40}")
#: Camera serial as used in folder names and run ids (``233100505``, ``U330439``,
#: ``SIM-0001``). No ``_``, because ``_`` separates the fields of a run id.
SERIAL_RE = re.compile(r"[A-Za-z0-9-]{1,32}")
#: One path component accepted by :func:`safe_join`: starts with a letter or
#: digit (so never ``.``/``..`` or a hidden file), then letters, digits, ``._-``.
COMPONENT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


class UnsafePathError(ValueError):
    """A path component or the resulting path is not allowed."""


# --------------------------------------------------------------------- roots
def ws_root() -> Path:
    """Return the workspace root (``WS/``)."""
    override = os.environ.get("CAMERA_LAB_WS_ROOT")
    return Path(override) if override else DEFAULT_WS_ROOT


def app_config_dir() -> Path:
    """Return ``APP/config/`` (git-tracked safety limits, scoring, lights, cameras)."""
    return APP_ROOT / "config"


def data_root() -> Path:
    """Return the run-record root (``WS/data/raw_recordings/camera_lab``)."""
    override = os.environ.get("CAMERA_LAB_DATA_ROOT")
    if override:
        return Path(override)
    return ws_root() / "data" / "raw_recordings" / "camera_lab"


def two_d_runs_root() -> Path:
    """Return the 2D run-record folder (``<data_root>/two_d``)."""
    return data_root() / "two_d"


def presets_root() -> Path:
    """Return the preset root (``WS/config/camera_presets``)."""
    return ws_root() / "config" / "camera_presets"


def two_d_presets_root() -> Path:
    """Return the 2D preset root (``WS/config/camera_presets/two_d``)."""
    return presets_root() / "two_d"


def camera_backups_root() -> Path:
    """Return the camera settings backup root (``WS/config/camera_backups``)."""
    return ws_root() / "config" / "camera_backups"


def calibration_root() -> Path:
    """Return ``WS/calibration`` (read-only for this app)."""
    return ws_root() / "calibration"


# ---------------------------------------------------------------- validation
def _validate(value: str, pattern: re.Pattern[str], what: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise UnsafePathError(f"invalid {what} {value!r}: must match {pattern.pattern}")
    return value


def validate_slug(value: str) -> str:
    """Return ``value`` if it is a valid slug (``[a-z0-9-]{1,40}``), else raise."""
    return _validate(value, SLUG_RE, "slug")


def validate_preset_name(value: str) -> str:
    """Return ``value`` if it is a valid preset name (``[a-z0-9_-]{1,40}``), else raise."""
    return _validate(value, PRESET_NAME_RE, "preset name")


def validate_serial(value: str) -> str:
    """Return ``value`` if it is a valid camera serial (``[A-Za-z0-9-]{1,32}``), else raise."""
    return _validate(value, SERIAL_RE, "camera serial")


def slugify(text: str, default: str = "run") -> str:
    """Derive a slug from free text (a note or goal); ``default`` if nothing is left."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40].strip("-")
    return slug or validate_slug(default)


def safe_join(base: Path, *parts: str) -> Path:
    """Join ``parts`` under ``base`` and guarantee the result stays inside ``base``.

    Each part must be exactly one path component matching :data:`COMPONENT_RE`
    (so no ``/``, ``..``, ``.``, NUL, absolute path, leading dot or empty part).
    The result is resolved (following symlinks) and must lie inside the resolved
    ``base``; a symlink that points outside ``base`` is rejected.

    Raises:
        UnsafePathError: if any check fails.
    """
    if not parts:
        raise UnsafePathError("safe_join needs at least one path component")
    for part in parts:
        if not isinstance(part, str):
            raise UnsafePathError(f"path component must be str, got {type(part).__name__}")
        if "\x00" in part:
            raise UnsafePathError("path component contains NUL")
        if os.path.isabs(part) or "/" in part or "\\" in part:
            raise UnsafePathError(f"path component {part!r} must be a single relative name")
        if part in (".", ".."):
            raise UnsafePathError(f"path component {part!r} is not allowed")
        _validate(part, COMPONENT_RE, "path component")
    base_resolved = Path(base).resolve()
    candidate = base_resolved.joinpath(*parts).resolve()
    if not candidate.is_relative_to(base_resolved) or candidate == base_resolved:
        raise UnsafePathError(f"path {candidate} escapes base {base_resolved}")
    return candidate
