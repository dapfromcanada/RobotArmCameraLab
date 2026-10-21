"""Shared test helper: a rendered chart page whose brightness follows the simulated camera.

Used by the setup procedure tests (backend and GUI).
"""

from __future__ import annotations

import cv2
import numpy as np

from camera_lab.two_d.charts import builtin_charts
from camera_lab.two_d.charts.draw import rasterize
from camera_lab.two_d.models import Frame
from camera_lab.two_d.service import TwoDService

PX = 2.5


def _page() -> np.ndarray:
    """Step wedge | grey patch | resolution, side by side (0–1 reflectance-like)."""
    pages = [rasterize(c, PX) for c in builtin_charts() if c.type != "tag_card"]
    h = max(p.shape[0] for p in pages)
    gap = np.ones((h, 30), np.uint8) * 255
    row = []
    for p in pages:
        row += [np.pad(p, ((0, h - p.shape[0]), (0, 0)), constant_values=255), gap]
    img = np.hstack(row[:-1]).astype(np.float64) / 255.0
    return cv2.GaussianBlur(img, (0, 0), 0.8)


class Scene:
    """Linear sensor: level = reflectance × k × exposure × gain × wb × colour cast."""

    def __init__(
        self, svc: TwoDService, k: float = 0.4 / 10_000, cast: tuple[float, ...] = (1.2, 1.0, 0.9)
    ) -> None:
        self.svc, self.k, self.cast = svc, k, np.array(cast)
        self.page = _page()
        self.drift = 0.0  # added to k each grab (unstable light)
        self.real_grab = svc.grab

    def grab(self, camera_id: str, timeout_ms: int = 2000) -> Frame:
        real = self.real_grab(camera_id, timeout_ms)
        s = real.settings
        wb = s.white_balance or {"red": 1.0, "green": 1.0, "blue": 1.0}
        gains = self.cast * np.array([wb["red"], wb["green"], wb["blue"]])
        self.k += self.drift
        level = self.page[..., None] * self.k * s.exposure_us * 10 ** (s.gain_db / 20) * gains
        data = np.clip(np.round(level * 255), 0, 255).astype(np.uint8)
        return real.model_copy(update={"data": data, "pixel_format": "RGB8", "bit_depth": 8})
