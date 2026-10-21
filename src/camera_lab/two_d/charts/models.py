"""Test-chart models (Phase 10): one ``config/charts/<chart_id>.yaml`` per chart.

Coordinates are millimetres on the printed page, origin at the top-left corner,
x to the right, y down. A tag's ``x_mm`` / ``y_mm`` is the top-left corner of
its black border; ``size_mm`` is the black border's width. Feature positions
relative to a tag are ``feature − tag`` in this frame.

Grey values are PDF device grey (0 = black ink, 1 = no ink / paper white). An
office print does not reproduce them as true reflectances: they are *nominal*
and fine for relative tests (camera A vs B, today vs last week).
"""

from __future__ import annotations

import math
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from camera_lab.core.paths import SLUG_RE

#: camera_lab charts use AprilTag 36h11 ids 100–199 (others belong to other apps).
TAG_FAMILY = "36h11"
TAG_ID_MIN = 100
TAG_ID_MAX = 199
#: An AprilTag 36h11 is 8 × 8 cells including the black border; it needs a white
#: quiet zone of at least one cell around it.
TAG_CELLS = 8

#: Paper sizes in mm (width, height).
PAPER_MM: dict[str, tuple[float, float]] = {"letter": (215.9, 279.4)}

ChartType = Literal["step_wedge", "resolution", "grey_patch", "tag_card"]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


Grey = Annotated[float, Field(ge=0.0, le=1.0)]
Mm = Annotated[float, Field(ge=0.0, le=2000.0)]
PositiveMm = Annotated[float, Field(gt=0.0, le=2000.0)]


class TagSpec(_Frozen):
    """One AprilTag 36h11 on the chart."""

    id: Annotated[int, Field(ge=TAG_ID_MIN, le=TAG_ID_MAX)]
    x_mm: Mm
    y_mm: Mm
    size_mm: PositiveMm

    @property
    def cell_mm(self) -> float:
        return self.size_mm / TAG_CELLS

    def quiet_box(self) -> tuple[float, float, float, float]:
        """(x0, y0, x1, y1) of the tag plus its one-cell white quiet zone."""
        c = self.cell_mm
        return (
            self.x_mm - c,
            self.y_mm - c,
            self.x_mm + self.size_mm + c,
            self.y_mm + self.size_mm + c,
        )


class PatchFeature(_Frozen):
    """A uniform rectangle (grey step, grey patch, black reference)."""

    kind: Literal["patch"] = "patch"
    name: str
    x_mm: Mm
    y_mm: Mm
    w_mm: PositiveMm
    h_mm: PositiveMm
    grey: Grey

    def bbox(self) -> tuple[float, float, float, float]:
        return (self.x_mm, self.y_mm, self.x_mm + self.w_mm, self.y_mm + self.h_mm)


class SlantedSquareFeature(_Frozen):
    """A dark square rotated by ``angle_deg``: four slanted edges for MTF."""

    kind: Literal["slanted_square"] = "slanted_square"
    name: str
    cx_mm: Mm
    cy_mm: Mm
    size_mm: PositiveMm
    angle_deg: Annotated[float, Field(ge=-45.0, le=45.0)]
    grey: Grey = 0.0

    def corners(self) -> tuple[tuple[float, float], ...]:
        a = math.radians(self.angle_deg)
        h = self.size_mm / 2
        pts = []
        for dx, dy in ((-h, -h), (h, -h), (h, h), (-h, h)):
            pts.append(
                (
                    self.cx_mm + dx * math.cos(a) - dy * math.sin(a),
                    self.cy_mm + dx * math.sin(a) + dy * math.cos(a),
                )
            )
        return tuple(pts)

    def bbox(self) -> tuple[float, float, float, float]:
        xs, ys = zip(*self.corners(), strict=True)
        return (min(xs), min(ys), max(xs), max(ys))


class StarFeature(_Frozen):
    """A Siemens star: ``spokes`` black wedges alternating with white."""

    kind: Literal["siemens_star"] = "siemens_star"
    name: str
    cx_mm: Mm
    cy_mm: Mm
    radius_mm: PositiveMm
    spokes: Annotated[int, Field(ge=4, le=180)]

    def bbox(self) -> tuple[float, float, float, float]:
        r = self.radius_mm
        return (self.cx_mm - r, self.cy_mm - r, self.cx_mm + r, self.cy_mm + r)


Feature = Annotated[PatchFeature | SlantedSquareFeature | StarFeature, Field(discriminator="kind")]


def _overlaps(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


class ChartSpec(_Frozen):
    """One chart of the chart library (``config/charts/<id>.yaml``)."""

    schema_version: Literal[1] = 1
    id: str
    type: ChartType
    version: Annotated[int, Field(ge=1, le=999)]
    title: Annotated[str, Field(min_length=1, max_length=80)]
    paper: Literal["letter"] = "letter"
    tags: tuple[TagSpec, ...] = Field(min_length=1, max_length=16)
    features: tuple[Feature, ...] = ()
    notes: tuple[Annotated[str, Field(max_length=300)], ...] = ()

    @field_validator("id")
    @classmethod
    def _slug(cls, v: str) -> str:
        if not SLUG_RE.fullmatch(v):
            raise ValueError(f"chart id must match {SLUG_RE.pattern}")
        return v

    @property
    def page_mm(self) -> tuple[float, float]:
        return PAPER_MM[self.paper]

    @model_validator(mode="after")
    def _geometry(self) -> ChartSpec:
        w, h = self.page_mm
        ids = [t.id for t in self.tags]
        if len(set(ids)) != len(ids):
            raise ValueError(f"duplicate tag ids in chart {self.id}: {ids}")
        names = [f.name for f in self.features]
        if len(set(names)) != len(names):
            raise ValueError(f"duplicate feature names in chart {self.id}")
        for t in self.tags:
            x0, y0, x1, y1 = t.quiet_box()
            if x0 < 0 or y0 < 0 or x1 > w or y1 > h:
                raise ValueError(f"tag {t.id} (with its quiet zone) does not fit on the page")
            for f in self.features:
                if _overlaps(t.quiet_box(), f.bbox()):
                    raise ValueError(f"feature {f.name!r} overlaps tag {t.id} or its quiet zone")
        for f in self.features:
            x0, y0, x1, y1 = f.bbox()
            if x0 < 0 or y0 < 0 or x1 > w or y1 > h:
                raise ValueError(f"feature {f.name!r} does not fit on the page")
        return self
