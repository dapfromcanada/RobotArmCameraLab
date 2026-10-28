"""The part area (Phase 13, Derek 2026-10-28): where the part is in the image, drawn once on
the live view and saved per camera in the catalog. Brightness scoring, the classic tuner and
the site light assessment then judge the part, not the whole scene.

The area is stored in full-sensor pixels, so it stays put when the camera's own ROI crops
the sensor; :func:`place` turns it into a scoring ROI (fractions) of the image as it is now.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import numpy.typing as npt

from camera_lab.two_d.catalog import PartArea
from camera_lab.two_d.models import ROI
from camera_lab.two_d.scoring import FractionalROI

#: Smallest part area (sensor pixels per side) that can be scored.
MIN_SIDE_PX = 8


@dataclass(frozen=True)
class Placement:
    """The part area in the current image: ``roi`` None = the whole image is scored."""

    roi: FractionalROI | None
    note: str | None = None


def place(area: PartArea, view: ROI) -> Placement:
    """The part area as a fraction of ``view`` (the camera ROI the image was taken with)."""
    x0, y0 = max(area.x, view.x), max(area.y, view.y)
    x1 = min(area.x + area.width, view.x + view.width)
    y1 = min(area.y + area.height, view.y + view.height)
    if x1 - x0 < MIN_SIDE_PX or y1 - y0 < MIN_SIDE_PX:
        return Placement(
            None,
            f"the part area ({describe(area)}) is outside this image (camera ROI "
            f"{view.x},{view.y} {view.width}×{view.height}): the whole image is scored",
        )
    roi = FractionalROI(
        x=(x0 - view.x) / view.width,
        y=(y0 - view.y) / view.height,
        width=(x1 - x0) / view.width,
        height=(y1 - y0) / view.height,
    )
    seen = (x1 - x0) * (y1 - y0) / (area.width * area.height)
    note = None
    if seen < 1:
        note = f"the part area is partly outside this image: the visible {seen:.0%} is scored"
    return Placement(roi, note)


def describe(area: PartArea) -> str:
    return f"{area.x},{area.y} {area.width}×{area.height} px"


def crop_plane(
    plane: npt.NDArray[np.float64], roi: FractionalROI | None
) -> npt.NDArray[np.float64]:
    """Crop a raw plane (Bayer mosaic kept intact: even offsets and sizes)."""
    if roi is None:
        return plane
    h, w = plane.shape[:2]
    x0, y0 = int(roi.x * w) // 2 * 2, int(roi.y * h) // 2 * 2
    x1 = min(w, round((roi.x + roi.width) * w) // 2 * 2)
    y1 = min(h, round((roi.y + roi.height) * h) // 2 * 2)
    return plane[y0:y1, x0:x1]
