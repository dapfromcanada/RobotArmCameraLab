"""The fingerprint bench (``APP/config/fingerprint_bench.yaml``): what the bench light's
settings give and which lenses are on the bench with their marked f-stops.

A fingerprint recipe (camera profile ``fingerprint:``) names a dimmer setting and an
f-number per step; this file turns that into an expected light level, which camera_lab
measures before it trusts the step (Phase 11 strict procedure, Derek 2026-10-22).
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from camera_lab.core.config import load_yaml_model
from camera_lab.core.paths import app_config_dir

BENCH_FILENAME = "fingerprint_bench.yaml"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class BenchLight(_Frozen):
    label: str = Field(max_length=200)
    #: Relative light per dimmer setting (the brightest setting = 1.0), measured.
    relative: dict[int, float] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def _positive(self) -> BenchLight:
        if any(v <= 0 or v > 10 for v in self.relative.values()):
            raise ValueError("relative light values must be in (0, 10]")
        return self


class BenchLens(_Frozen):
    label: str = Field(max_length=120)
    f_stops: tuple[float, ...] = Field(min_length=1, max_length=20)
    #: Measured light at a marked stop relative to fully open (fast lenses pass less light
    #: wide open than the f-number says, so closing them cuts less than (f_open / f)²).
    transmission: dict[float, float] = Field(default_factory=dict, max_length=20)

    @model_validator(mode="after")
    def _transmission_ok(self) -> BenchLens:
        bad = [f for f, t in self.transmission.items() if f not in self.f_stops or not 0 < t <= 1]
        if bad:
            raise ValueError(f"transmission needs marked stops and values in (0, 1]: {bad}")
        return self

    @property
    def open_f(self) -> float:
        """The fully open f-number (the smallest marked stop)."""
        return min(self.f_stops)

    def relative(self, f_number: float) -> float:
        """Light at ``f_number`` relative to fully open: measured where known, else nominal
        from the nearest measured stop at or below it."""
        if f_number in self.transmission:
            return self.transmission[f_number]
        known = {self.open_f: 1.0, **self.transmission}
        base = max((f for f in known if f <= f_number), default=self.open_f)
        return known[base] * (base / f_number) ** 2


class Bench(_Frozen):
    schema_version: int = Field(ge=1, le=1)
    light: BenchLight
    #: Shortest exposure for lit measurements (the bench light's ripple averages out above it).
    min_exposure_us: float = Field(default=2000.0, ge=0, le=1_000_000)
    #: The same for global-shutter cameras: ripple there is not row bands, only a little
    #: frame-to-frame variation, so shorter exposures are clean (None: use min_exposure_us).
    min_exposure_global_us: float | None = Field(default=None, ge=0, le=1_000_000)
    lenses: dict[str, BenchLens] = Field(max_length=50)
    notes: tuple[str, ...] = ()

    def light_ratio(
        self,
        dimmer: int,
        f_number: float,
        ref_dimmer: int,
        ref_f: float,
        lens: BenchLens | None = None,
    ) -> float:
        """Light at (dimmer, f_number) relative to (ref_dimmer, ref_f); with ``lens``, its
        measured transmission replaces the nominal (ref_f / f)²."""
        rel = self.light.relative
        for d in (dimmer, ref_dimmer):
            if d not in rel:
                raise ValueError(f"the bench light has no setting {d}; known: {sorted(rel)}")
        iris = (
            lens.relative(f_number) / lens.relative(ref_f)
            if lens is not None
            else (ref_f / f_number) ** 2
        )
        return rel[dimmer] / rel[ref_dimmer] * iris


def load_bench(path: Path | None = None) -> Bench | None:
    """The bench file, or None when there is none."""
    p = path if path is not None else app_config_dir() / BENCH_FILENAME
    if not p.exists():
        return None
    return load_yaml_model(p, Bench)


def lens_choices(
    camera_model: str, bench: Bench | None, catalog_path: Path | None = None
) -> list[tuple[str, BenchLens, bool]]:
    """The lenses to pick from for a camera model: (id, lens, tested on this model), those
    tested on the model first. From the catalog (the source of truth, Derek 2026-10-25); the
    bench file's own list only while the catalog holds no lenses (a fresh workspace)."""
    from camera_lab.two_d.catalog import Catalog

    with Catalog(catalog_path) as cat:
        infos = cat.lenses_for_model(camera_model)
    out: list[tuple[str, BenchLens, bool]] = []
    for info in infos:
        stops = tuple(s.f_number for s in info.stops)
        if not stops:
            continue
        trans = {
            s.f_number: float(s.transmission)
            for s in info.stops
            if s.transmission is not None and s.f_number != min(stops) and 0 < s.transmission <= 1
        }
        out.append(
            (
                info.id,
                BenchLens(label=info.name, f_stops=stops, transmission=trans),
                camera_model in info.tested_on,
            )
        )
    if not out and bench is not None:
        out = [(lid, lens, False) for lid, lens in bench.lenses.items()]
    return out
