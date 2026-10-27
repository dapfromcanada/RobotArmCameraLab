"""The app page for other agents is built from the app's own paths."""

from __future__ import annotations

from pathlib import Path

import pytest

from camera_lab.core.manual import manual_path, write_manual


def test_page_lists_every_file_the_app_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CAMERA_LAB_WS_ROOT", str(tmp_path))
    monkeypatch.delenv("CAMERA_LAB_CATALOG", raising=False)
    monkeypatch.delenv("CAMERA_LAB_CATALOG_BACKUPS", raising=False)
    monkeypatch.delenv("CAMERA_LAB_DATA_ROOT", raising=False)
    out = write_manual()
    assert out == manual_path() == tmp_path / "Manuals" / "apps" / "camera_lab.md"
    page = out.read_text()
    assert "**Revision:** 0." in page
    for path in (
        "WS/config/camera_catalog.sqlite",
        "WS/config/camera_catalog_backups",
        "WS/config/camera_fingerprints",
        "WS/config/camera_backups",
        "WS/data/raw_recordings/camera_lab",
    ):
        assert f"`{path}`" in page
    catalog_row = next(line for line in page.splitlines() if "camera_catalog.sqlite`" in line)
    assert "**source of truth.**" in catalog_row and "| database |" in catalog_row
