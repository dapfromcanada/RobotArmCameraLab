"""The camera and lens catalog (Phase 11, Derek 2026-10-25): one SQLite database, the single
source of truth for camera models, physical cameras, lenses, camera + lens pairs, fingerprint
runs and every measured result. White papers are generated from it.

- :mod:`schema` — tables and numbered migrations
- :mod:`store` — :class:`Catalog`, the only access (every change goes to ``change_log``)
- :mod:`importer` — loads the bench file, camera profiles and fingerprint folders
"""

from camera_lab.two_d.catalog.store import (
    STANDARD_F_STOPS,
    Catalog,
    CatalogError,
    LensInfo,
    Result,
    catalog_backups_dir,
    catalog_path,
)

__all__ = [
    "STANDARD_F_STOPS",
    "Catalog",
    "CatalogError",
    "LensInfo",
    "Result",
    "catalog_backups_dir",
    "catalog_path",
]
