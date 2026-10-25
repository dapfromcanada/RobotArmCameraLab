"""Catalog tables. Plain SQL (no SQLite-only types) so the database can move to PostgreSQL
by copying. Each migration is applied once, in order, inside one transaction."""

from __future__ import annotations

import sqlite3

#: Migration n turns schema version n - 1 into n.
MIGRATIONS: tuple[str, ...] = (
    """
    CREATE TABLE schema_version (version INTEGER NOT NULL);

    CREATE TABLE camera_models (
        id TEXT PRIMARY KEY,                 -- e.g. TRI054S-C
        vendor TEXT,
        sensor TEXT,
        shutter TEXT CHECK (shutter IN ('rolling', 'global', 'unknown')),
        colour TEXT,                         -- bayer, prism, mono, unknown
        hdr INTEGER NOT NULL DEFAULT 0,
        design_mode TEXT,
        notes TEXT
    );

    CREATE TABLE cameras (
        serial TEXT PRIMARY KEY,
        model_id TEXT NOT NULL REFERENCES camera_models (id),
        firmware TEXT,
        location TEXT,                       -- bench, a cell, ...
        notes TEXT
    );

    CREATE TABLE lenses (
        id TEXT PRIMARY KEY,                 -- e.g. kowa_8mm
        name TEXT NOT NULL,                  -- as printed on the barrel
        focal_mm REAL,
        mount TEXT,
        notes TEXT
    );

    CREATE TABLE lens_stops (
        lens_id TEXT NOT NULL REFERENCES lenses (id) ON DELETE CASCADE,
        f_number REAL NOT NULL,
        printed INTEGER NOT NULL DEFAULT 1,  -- 0: a dot between marks (not used by procedures)
        end_stop TEXT CHECK (end_stop IN ('open', 'closed')),
        transmission REAL,                   -- measured light vs fully open (NULL: not measured)
        fingerprint_id TEXT REFERENCES fingerprints (id),
        measured_utc TEXT,
        PRIMARY KEY (lens_id, f_number)
    );

    CREATE TABLE pairs (
        model_id TEXT NOT NULL REFERENCES camera_models (id),
        lens_id TEXT NOT NULL REFERENCES lenses (id),
        notes TEXT,
        PRIMARY KEY (model_id, lens_id)
    );

    CREATE TABLE pair_stops (
        model_id TEXT NOT NULL,
        lens_id TEXT NOT NULL,
        f_number REAL NOT NULL,
        plane TEXT NOT NULL,                 -- R, G, B, Gr, Gb, or 'all'
        corner_ratio REAL,                   -- corners / centre
        fingerprint_id TEXT REFERENCES fingerprints (id),
        PRIMARY KEY (model_id, lens_id, f_number, plane),
        FOREIGN KEY (model_id, lens_id) REFERENCES pairs (model_id, lens_id)
    );

    CREATE TABLE fingerprints (
        id TEXT PRIMARY KEY,                 -- <serial>/<stamp>
        serial TEXT NOT NULL REFERENCES cameras (serial),
        kind TEXT NOT NULL CHECK (kind IN ('fingerprint', 'live_check')),
        lens_id TEXT REFERENCES lenses (id),
        lens_text TEXT,                      -- as recorded with the run
        f_number REAL,                       -- the reference iris of the run
        started_utc TEXT NOT NULL,
        status TEXT NOT NULL,                -- pass, partial, fail
        valid INTEGER NOT NULL DEFAULT 1,    -- 0: kept for history, not used for papers
        note TEXT,
        folder TEXT,                         -- raw data, master darks / flats, the run's report
        temperature_min_c REAL,
        temperature_max_c REAL,
        imported_utc TEXT NOT NULL
    );

    CREATE TABLE results (
        id INTEGER PRIMARY KEY,
        fingerprint_id TEXT NOT NULL REFERENCES fingerprints (id) ON DELETE CASCADE,
        quantity TEXT NOT NULL,              -- e.g. read_noise_dn
        value REAL,
        unit TEXT,
        gain_db REAL,
        exposure_us REAL,
        gain_mode TEXT,
        f_number REAL,
        plane TEXT,
        channel TEXT,
        pixel_format TEXT,
        signal_dn REAL,
        setting TEXT,
        spread_pct REAL,                     -- repeat spread where repeated
        note TEXT
    );
    CREATE INDEX results_by_run ON results (fingerprint_id, quantity);

    CREATE TABLE change_log (
        id INTEGER PRIMARY KEY,
        utc TEXT NOT NULL,
        actor TEXT NOT NULL,                 -- human, agent, fingerprint, import
        tbl TEXT NOT NULL,
        row_key TEXT NOT NULL,
        col TEXT,
        old TEXT,
        new TEXT,
        reason TEXT
    );

    INSERT INTO schema_version (version) VALUES (0);
    """,
)

SCHEMA_VERSION = len(MIGRATIONS)


def current_version(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'schema_version'"
    ).fetchone()
    if row is None:
        return 0
    return int(conn.execute("SELECT version FROM schema_version").fetchone()[0])


def migrate(conn: sqlite3.Connection) -> int:
    """Apply the missing migrations; return the schema version."""
    version = current_version(conn)
    if version > SCHEMA_VERSION:
        raise RuntimeError(
            f"catalog schema {version} is newer than this camera_lab ({SCHEMA_VERSION})"
        )
    for n in range(version, SCHEMA_VERSION):
        try:
            # constant SQL from this module; the version is an int
            script = f"BEGIN;{MIGRATIONS[n]}UPDATE schema_version SET version = {n + 1};COMMIT;"  # noqa: S608
            conn.executescript(script)
        except sqlite3.Error:
            conn.rollback()
            raise
    return SCHEMA_VERSION
