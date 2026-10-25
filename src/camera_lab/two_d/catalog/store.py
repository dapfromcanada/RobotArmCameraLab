"""The catalog's only access: :class:`Catalog`. Every change to a catalog row is written to
``change_log`` (who, when, which column, old → new); results are append-only per run."""

from __future__ import annotations

import os
import re
import sqlite3
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from camera_lab.core.paths import ws_root
from camera_lab.two_d.catalog.schema import migrate
from camera_lab.two_d.errors import InvalidRequestError

CATALOG_FILENAME = "camera_catalog.sqlite"
#: Marked f-stops a lens ring can carry (Derek 2026-10-25: picked, never typed).
STANDARD_F_STOPS = (1.0, 1.4, 2.0, 2.8, 4.0, 5.6, 8.0, 11.0, 16.0, 22.0, 32.0)
MOUNTS = ("C", "CS", "M12", "F", "EF", "M42", "other")
ID_RE = re.compile(r"[a-z0-9][a-z0-9_]{0,39}")
MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
Actor = Literal["human", "agent", "fingerprint", "import"]


class CatalogError(InvalidRequestError):
    """A catalog request that cannot be done (unknown id, invalid value)."""


def catalog_path() -> Path:
    """``CAMERA_LAB_CATALOG`` or ``WS/config/camera_catalog.sqlite``."""
    override = os.environ.get("CAMERA_LAB_CATALOG")
    return Path(override) if override else ws_root() / "config" / CATALOG_FILENAME


def _utc() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def lens_id_for(name: str, focal_mm: float | None = None) -> str:
    """A lens id from its printed name: ``JAI 6 mm 0628-C3`` → ``jai_6mm_0628_c3``."""
    text = re.sub(r"(\d)\s*mm\b", r"\1mm", name.lower())
    out = re.sub(r"[^a-z0-9]+", "_", text).strip("_")[:40]
    if not out or ID_RE.fullmatch(out) is None:
        raise CatalogError(f"cannot make a lens id from {name!r}")
    return out


@dataclass(frozen=True)
class Result:
    """One measured value of a run, with the conditions it was measured at."""

    quantity: str
    value: float | None
    unit: str | None = None
    gain_db: float | None = None
    exposure_us: float | None = None
    gain_mode: str | None = None
    f_number: float | None = None
    plane: str | None = None
    channel: str | None = None
    pixel_format: str | None = None
    signal_dn: float | None = None
    setting: str | None = None
    spread_pct: float | None = None
    note: str | None = None


#: Constant column names (the dataclass fields = the ``results`` table's condition columns).
RESULT_COLUMNS = tuple(Result.__dataclass_fields__)


@dataclass(frozen=True)
class LensStop:
    f_number: float
    printed: bool
    end_stop: str | None
    transmission: float | None


@dataclass(frozen=True)
class LensInfo:
    id: str
    name: str
    focal_mm: float | None
    mount: str | None
    notes: str | None
    stops: tuple[LensStop, ...] = ()
    tested_on: tuple[str, ...] = field(default=())  # camera models with a pair

    @property
    def open_f(self) -> float | None:
        return min((s.f_number for s in self.stops), default=None)

    @property
    def closed_f(self) -> float | None:
        return max((s.f_number for s in self.stops), default=None)

    def label(self, f_number: float | None = None) -> str:
        return self.name if f_number is None else f"{self.name}, f/{f_number:g}"


def _check_names(conn: sqlite3.Connection, table: str, columns: Iterable[str]) -> None:
    """Guard for the identifiers placed in SQL by :meth:`Catalog._write`."""
    known = {str(r[0]) for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if table not in known:
        raise CatalogError(f"unknown catalog table {table!r}")
    cols = {str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})")}
    bad = [c for c in columns if c not in cols]
    if bad:
        raise CatalogError(f"unknown columns of {table}: {', '.join(bad)}")


class Catalog:
    """One connection to the catalog database (created and migrated on first use)."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path if path is not None else catalog_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, timeout=10.0, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self.version = migrate(self._conn)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Catalog:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # ------------------------------------------------------------ plumbing
    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """One change = one transaction (the GUI, CLI and MCP may write at the same time)."""
        conn = self._conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")

    def _write(
        self,
        conn: sqlite3.Connection,
        table: str,
        key: Mapping[str, Any],
        values: Mapping[str, Any],
        actor: Actor,
        reason: str | None,
    ) -> bool:
        """Insert or update one row; log every changed column. True if anything changed.

        Table and column names come only from this module's code; they are checked against
        the schema before being placed in SQL (values always go through ``?``)."""
        _check_names(conn, table, (*key, *values))
        where = " AND ".join(f"{k} = ?" for k in key)
        old = conn.execute(
            f"SELECT * FROM {table} WHERE {where}",  # noqa: S608 - names checked above
            tuple(key.values()),
        ).fetchone()
        row_key = "/".join(str(v) for v in key.values())
        now = _utc()
        if old is None:
            cols = {**key, **values}
            conn.execute(
                f"INSERT INTO {table} ({', '.join(cols)}) "  # noqa: S608 - names checked above
                f"VALUES ({', '.join('?' * len(cols))})",
                tuple(cols.values()),
            )
            conn.execute(
                "INSERT INTO change_log (utc, actor, tbl, row_key, col, old, new, reason) "
                "VALUES (?, ?, ?, ?, NULL, NULL, 'created', ?)",
                (now, actor, table, row_key, reason),
            )
            return True
        changed = {c: v for c, v in values.items() if old[c] != v}
        if not changed:
            return False
        conn.execute(
            f"UPDATE {table} SET {', '.join(f'{c} = ?' for c in changed)} "  # noqa: S608
            f"WHERE {where}",
            (*changed.values(), *key.values()),
        )
        conn.executemany(
            "INSERT INTO change_log (utc, actor, tbl, row_key, col, old, new, reason) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    now,
                    actor,
                    table,
                    row_key,
                    c,
                    None if old[c] is None else str(old[c]),
                    None if v is None else str(v),
                    reason,
                )
                for c, v in changed.items()
            ],
        )
        return True

    # ------------------------------------------------------- camera models
    def upsert_camera_model(
        self,
        model_id: str,
        *,
        vendor: str | None = None,
        sensor: str | None = None,
        shutter: str = "unknown",
        colour: str = "unknown",
        hdr: bool = False,
        design_mode: str | None = None,
        notes: str | None = None,
        actor: Actor = "human",
        reason: str | None = None,
    ) -> bool:
        if MODEL_RE.fullmatch(model_id) is None:
            raise CatalogError(f"invalid camera model {model_id!r}")
        with self.transaction() as conn:
            return self._write(
                conn,
                "camera_models",
                {"id": model_id},
                {
                    "vendor": vendor,
                    "sensor": sensor,
                    "shutter": shutter,
                    "colour": colour,
                    "hdr": int(hdr),
                    "design_mode": design_mode,
                    "notes": notes,
                },
                actor,
                reason,
            )

    def upsert_camera(
        self,
        serial: str,
        model_id: str,
        *,
        firmware: str | None = None,
        location: str | None = None,
        notes: str | None = None,
        actor: Actor = "human",
        reason: str | None = None,
    ) -> bool:
        with self.transaction() as conn:
            if (
                conn.execute("SELECT 1 FROM camera_models WHERE id = ?", (model_id,)).fetchone()
                is None
            ):
                raise CatalogError(f"unknown camera model {model_id!r}")
            return self._write(
                conn,
                "cameras",
                {"serial": serial},
                {"model_id": model_id, "firmware": firmware, "location": location, "notes": notes},
                actor,
                reason,
            )

    def camera_models(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self._conn.execute("SELECT * FROM camera_models ORDER BY id")]

    def cameras(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self._conn.execute("SELECT * FROM cameras ORDER BY serial")]

    def model_of(self, serial: str) -> str | None:
        row = self._conn.execute(
            "SELECT model_id FROM cameras WHERE serial = ?", (serial,)
        ).fetchone()
        return str(row[0]) if row is not None else None

    # --------------------------------------------------------------- lenses
    def add_lens(
        self,
        name: str,
        *,
        focal_mm: float | None,
        mount: str | None,
        stops: Iterable[float],
        dots: Iterable[float] = (),
        notes: str | None = None,
        lens_id: str | None = None,
        actor: Actor = "human",
        reason: str | None = None,
    ) -> str:
        """A lens and its marked stops (from the standard list); ``dots`` are stops between
        marks (kept where measured, never used by procedures). Returns the lens id."""
        name = " ".join(name.split())
        if not name or len(name) > 120:
            raise CatalogError("the lens name (as printed on the barrel) is required, ≤ 120 chars")
        lid = lens_id or lens_id_for(name)
        if ID_RE.fullmatch(lid) is None:
            raise CatalogError(f"invalid lens id {lid!r}")
        if focal_mm is not None and not 0.5 <= focal_mm <= 2000:
            raise CatalogError(f"focal length {focal_mm} mm is out of range")
        if mount is not None and mount not in MOUNTS:
            raise CatalogError(f"mount {mount!r}: pick one of {', '.join(MOUNTS)}")
        marked = sorted({float(f) for f in stops})
        extra = sorted({float(f) for f in dots} - set(marked))
        bad = [f for f in (*marked, *extra) if f not in STANDARD_F_STOPS]
        if bad or len(marked) < 1:
            raise CatalogError(
                f"stops must be picked from {', '.join(f'{f:g}' for f in STANDARD_F_STOPS)}"
                + (f" (not {', '.join(f'{f:g}' for f in bad)})" if bad else "")
            )
        everything = sorted([*marked, *extra])
        with self.transaction() as conn:
            self._write(
                conn,
                "lenses",
                {"id": lid},
                {"name": name, "focal_mm": focal_mm, "mount": mount, "notes": notes},
                actor,
                reason,
            )
            for f in everything:
                end = "open" if f == marked[0] else "closed" if f == marked[-1] else None
                self._write(
                    conn,
                    "lens_stops",
                    {"lens_id": lid, "f_number": f},
                    {"printed": int(f in marked), "end_stop": end},
                    actor,
                    reason,
                )
        return lid

    def set_transmission(
        self,
        lens_id: str,
        f_number: float,
        transmission: float,
        *,
        fingerprint_id: str | None = None,
        measured_utc: str | None = None,
        actor: Actor = "fingerprint",
        reason: str | None = None,
    ) -> None:
        if not 0 < transmission <= 1.5:
            raise CatalogError(f"transmission {transmission} out of range")
        with self.transaction() as conn:
            if (
                conn.execute(
                    "SELECT 1 FROM lens_stops WHERE lens_id = ? AND f_number = ?",
                    (lens_id, f_number),
                ).fetchone()
                is None
            ):
                raise CatalogError(f"lens {lens_id!r} has no stop f/{f_number:g}")
            self._write(
                conn,
                "lens_stops",
                {"lens_id": lens_id, "f_number": f_number},
                {
                    "transmission": transmission,
                    "fingerprint_id": fingerprint_id,
                    "measured_utc": measured_utc or _utc(),
                },
                actor,
                reason,
            )

    def lens(self, lens_id: str) -> LensInfo | None:
        row = self._conn.execute("SELECT * FROM lenses WHERE id = ?", (lens_id,)).fetchone()
        if row is None:
            return None
        stops = tuple(
            LensStop(
                f_number=float(s["f_number"]),
                printed=bool(s["printed"]),
                end_stop=s["end_stop"],
                transmission=s["transmission"],
            )
            for s in self._conn.execute(
                "SELECT * FROM lens_stops WHERE lens_id = ? ORDER BY f_number", (lens_id,)
            )
        )
        tested = tuple(
            str(r[0])
            for r in self._conn.execute(
                "SELECT model_id FROM pairs WHERE lens_id = ? ORDER BY model_id", (lens_id,)
            )
        )
        return LensInfo(
            id=row["id"],
            name=row["name"],
            focal_mm=row["focal_mm"],
            mount=row["mount"],
            notes=row["notes"],
            stops=tuple(s for s in stops if s.printed),
            tested_on=tested,
        )

    def lenses(self) -> list[LensInfo]:
        ids = [str(r[0]) for r in self._conn.execute("SELECT id FROM lenses ORDER BY name")]
        return [lens for i in ids if (lens := self.lens(i)) is not None]

    def lenses_for_model(self, model_id: str) -> list[LensInfo]:
        """The GUI pick list: lenses already tested on this camera model first, then the rest."""
        all_lenses = self.lenses()
        tested = [x for x in all_lenses if model_id in x.tested_on]
        return tested + [x for x in all_lenses if model_id not in x.tested_on]

    def find_lens(self, text: str) -> LensInfo | None:
        """The lens whose printed name starts a recorded lens / iris text."""
        t = " ".join(text.split()).lower()
        hits = [x for x in self.lenses() if t.startswith(x.name.lower())]
        return max(hits, key=lambda x: len(x.name)) if hits else None

    # ---------------------------------------------------------------- pairs
    def upsert_pair(
        self,
        model_id: str,
        lens_id: str,
        *,
        notes: str | None = None,
        actor: Actor = "human",
        reason: str | None = None,
    ) -> bool:
        with self.transaction() as conn:
            return self._write(
                conn,
                "pairs",
                {"model_id": model_id, "lens_id": lens_id},
                {"notes": notes},
                actor,
                reason,
            )

    def set_pair_stop(
        self,
        model_id: str,
        lens_id: str,
        f_number: float,
        plane: str,
        corner_ratio: float,
        *,
        fingerprint_id: str | None = None,
        actor: Actor = "fingerprint",
        reason: str | None = None,
    ) -> None:
        with self.transaction() as conn:
            if (
                conn.execute(
                    "SELECT 1 FROM pairs WHERE model_id = ? AND lens_id = ?", (model_id, lens_id)
                ).fetchone()
                is None
            ):
                self._write(
                    conn,
                    "pairs",
                    {"model_id": model_id, "lens_id": lens_id},
                    {"notes": None},
                    actor,
                    reason,
                )
            self._write(
                conn,
                "pair_stops",
                {"model_id": model_id, "lens_id": lens_id, "f_number": f_number, "plane": plane},
                {"corner_ratio": corner_ratio, "fingerprint_id": fingerprint_id},
                actor,
                reason,
            )

    def pair_stops(self, model_id: str, lens_id: str) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self._conn.execute(
                "SELECT * FROM pair_stops WHERE model_id = ? AND lens_id = ? "
                "ORDER BY f_number, plane",
                (model_id, lens_id),
            )
        ]

    # --------------------------------------------------------- fingerprints
    def add_run(
        self,
        run_id: str,
        serial: str,
        *,
        kind: Literal["fingerprint", "live_check"],
        started_utc: str,
        status: str,
        results: Iterable[Result],
        lens_id: str | None = None,
        lens_text: str | None = None,
        f_number: float | None = None,
        valid: bool = True,
        note: str | None = None,
        folder: str | None = None,
        temperature_min_c: float | None = None,
        temperature_max_c: float | None = None,
        actor: Actor = "fingerprint",
    ) -> int:
        """A run and its results (replaced when the run is imported again). Returns the
        number of results."""
        rows = list(results)
        with self.transaction() as conn:
            if conn.execute("SELECT 1 FROM cameras WHERE serial = ?", (serial,)).fetchone() is None:
                raise CatalogError(f"unknown camera {serial!r}: add it first")
            prev = conn.execute(
                "SELECT imported_utc FROM fingerprints WHERE id = ?", (run_id,)
            ).fetchone()
            conn.execute("DELETE FROM results WHERE fingerprint_id = ?", (run_id,))
            self._write(
                conn,
                "fingerprints",
                {"id": run_id},
                {
                    "serial": serial,
                    "kind": kind,
                    "lens_id": lens_id,
                    "lens_text": lens_text,
                    "f_number": f_number,
                    "started_utc": started_utc,
                    "status": status,
                    "valid": int(valid),
                    "note": note,
                    "folder": folder,
                    "temperature_min_c": temperature_min_c,
                    "temperature_max_c": temperature_max_c,
                    # first import only: re-importing an unchanged run changes nothing
                    "imported_utc": (prev["imported_utc"] if prev is not None else _utc()),
                },
                actor,
                None,
            )
            conn.executemany(
                f"INSERT INTO results (fingerprint_id, {', '.join(RESULT_COLUMNS)}) "  # noqa: S608
                f"VALUES (?, {', '.join('?' * len(RESULT_COLUMNS))})",
                [(run_id, *(getattr(r, c) for c in RESULT_COLUMNS)) for r in rows],
            )
            conn.execute(
                "INSERT INTO change_log (utc, actor, tbl, row_key, col, old, new, reason) "
                "VALUES (?, ?, 'results', ?, NULL, NULL, ?, NULL)",
                (_utc(), actor, run_id, f"{len(rows)} results"),
            )
        return len(rows)

    def runs(self, serial: str | None = None, *, valid_only: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM fingerprints"
        cond, args = [], []
        if serial is not None:
            cond.append("serial = ?")
            args.append(serial)
        if valid_only:
            cond.append("valid = 1")
        if cond:
            sql += " WHERE " + " AND ".join(cond)
        return [dict(r) for r in self._conn.execute(sql + " ORDER BY started_utc", args)]

    def results(self, run_id: str, quantity: str | None = None) -> list[Result]:
        sql = f"SELECT {', '.join(RESULT_COLUMNS)} FROM results WHERE fingerprint_id = ?"  # noqa: S608
        args: list[Any] = [run_id]
        if quantity is not None:
            sql += " AND quantity = ?"
            args.append(quantity)
        return [Result(**dict(r)) for r in self._conn.execute(sql + " ORDER BY id", args)]

    def model_results(self, model_id: str, quantity: str) -> list[tuple[str, Result]]:
        """Every valid run's value of ``quantity`` for a camera model: (run id, result)."""
        rows = self._conn.execute(
            f"SELECT f.id AS run_id, {', '.join('r.' + c for c in RESULT_COLUMNS)} "  # noqa: S608
            "FROM results r JOIN fingerprints f ON f.id = r.fingerprint_id "
            "JOIN cameras c ON c.serial = f.serial "
            "WHERE c.model_id = ? AND r.quantity = ? AND f.valid = 1 ORDER BY f.started_utc, r.id",
            (model_id, quantity),
        )
        return [(r["run_id"], Result(**{c: r[c] for c in RESULT_COLUMNS})) for r in rows]

    def changes(self, limit: int = 50) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self._conn.execute(
                "SELECT * FROM change_log ORDER BY id DESC LIMIT ?", (limit,)
            )
        ]
