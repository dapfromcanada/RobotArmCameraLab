"""Shared test helper: the nine field-chart sheets on three column boards, as a camera sees
them, with blur, light and colour error that change across the image."""

from __future__ import annotations

import math
from collections.abc import Callable

import cv2
import numpy as np
import numpy.typing as npt

from camera_lab.two_d.charts import field_charts
from camera_lab.two_d.charts.draw import rasterize

PX = 2.0  # px per mm on the board
GAP_MM = 25.0  # between the column boards
MARGIN_MM = 12.0
POS = {  # role → (column, row)
    "top-left": (0, 0), "top": (1, 0), "top-right": (2, 0),
    "left": (0, 1), "centre": (1, 1), "right": (2, 1),
    "bottom-left": (0, 2), "bottom": (1, 2), "bottom-right": (2, 2),
}  # fmt: skip


def board(
    sigma: Callable[[float], float] = lambda r: 0.8,
    side_extra_x: float = 0.0,
    light: Callable[[float], float] = lambda r: 1.0,
    missing: tuple[str, ...] = (),
    shift_px: dict[str, tuple[int, int]] | None = None,
    left_extra: float = 0.0,
    px: float = PX,
) -> npt.NDArray[np.float64]:
    """Linear 0–1 mono image. ``sigma(r)``: blur at field radius r; ``side_extra_x``: more
    blur along x on the left / right sheets (their tangential edges); ``light(r)``: light;
    ``shift_px``: sheets placed off their grid spot; ``left_extra``: more blur on the left
    column (a tilted lens or sensor)."""
    sheets = {c.role: rasterize(c, px).astype(np.float64) / 255.0 for c in field_charts()}
    sh, sw = next(iter(sheets.values())).shape
    gap, margin = round(GAP_MM * px), round(MARGIN_MM * px)
    h, w = 3 * sh + 2 * margin, 3 * sw + 2 * gap + 2 * margin
    img = np.ones((h, w))
    half_diag = math.hypot(w, h) / 2
    for role, (col, row) in POS.items():
        if role in missing:
            continue
        dx, dy = (shift_px or {}).get(role, (0, 0))
        x0, y0 = margin + col * (sw + gap) + dx, margin + row * sh + dy
        r = math.hypot(x0 + sw / 2 - w / 2, y0 + sh / 2 - h / 2) / half_diag
        extra = left_extra if col == 0 else 0.0
        page = cv2.GaussianBlur(sheets[role], (0, 0), math.hypot(sigma(r), extra))
        if side_extra_x and role in ("left", "right"):
            page = cv2.GaussianBlur(page, (0, 0), sigmaX=side_extra_x, sigmaY=1e-3)
        img[y0 : y0 + sh, x0 : x0 + sw] = page * light(r)
    return 0.04 + 0.6 * img


def colour(mono: npt.NDArray[np.float64], red_scale: float) -> npt.NDArray[np.float64]:
    """RGB with the red plane scaled about the image centre (lateral colour error)."""
    h, w = mono.shape
    m = cv2.getRotationMatrix2D(((w - 1) / 2, (h - 1) / 2), 0.0, red_scale)
    red = cv2.warpAffine(mono, m, (w, h), flags=cv2.INTER_CUBIC, borderValue=float(mono.max()))
    return np.dstack([red, mono, mono])


def to8(img: npt.NDArray[np.float64]) -> npt.NDArray[np.uint8]:
    return np.clip(np.round(img * 255), 0, 255).astype(np.uint8)
