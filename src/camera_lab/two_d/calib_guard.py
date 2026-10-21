"""Read-only calibration guard.

When a setting that affects calibration changes (ROI, binning, decimation,
reverse X/Y, a resolution-changing pixel format, focus, zoom), look for
existing calibration files for that camera and return warnings:

    "this change may invalidate calibration in <path>"

Files are matched by name only (``WS/calibration/intrinsics/<serial>*`` and
``WS/calibration/extrinsics/<serial>*``). Nothing is opened, parsed or written:
the format belongs to the separate calibration app.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from camera_lab.core.paths import calibration_root, validate_serial

#: Setting names whose change may invalidate an existing calibration.
CALIBRATION_SETTINGS = frozenset(
    {"roi", "binning", "decimation", "reverse_x", "reverse_y", "resolution", "focus", "zoom"}
)
CALIBRATION_KINDS = ("intrinsics", "extrinsics")


def calibration_files(serial: str, root: Path | None = None) -> list[Path]:
    """Return existing calibration entries for ``serial`` (sorted; read-only lookup)."""
    validate_serial(serial)
    base = Path(root) if root is not None else calibration_root()
    found: list[Path] = []
    for kind in CALIBRATION_KINDS:
        d = base / kind
        if d.is_dir():
            found.extend(p for p in d.glob(f"{serial}*") if not p.name.startswith("."))
    return sorted(found)


def calibration_warnings(
    serial: str, changed: Iterable[str], root: Path | None = None
) -> list[str]:
    """Return one warning per calibration entry if any changed setting affects calibration."""
    if not CALIBRATION_SETTINGS.intersection(changed):
        return []
    return [
        f"this change may invalidate calibration in {p}" for p in calibration_files(serial, root)
    ]
