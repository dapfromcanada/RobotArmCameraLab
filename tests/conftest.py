"""Shared pytest fixtures.

Every test runs against a temporary workspace/data root so nothing is written to
the real ``WS/data`` or ``WS/config``. Sim camera / sim light fixtures are added
in later phases.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(autouse=True)
def tmp_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point CAMERA_LAB_WS_ROOT and CAMERA_LAB_DATA_ROOT at a temp folder."""
    ws = tmp_path / "ws"
    data = ws / "data" / "raw_recordings" / "camera_lab"
    data.mkdir(parents=True)
    monkeypatch.setenv("CAMERA_LAB_WS_ROOT", str(ws))
    monkeypatch.setenv("CAMERA_LAB_DATA_ROOT", str(data))
    monkeypatch.delenv("CAMERA_LAB_CATALOG", raising=False)  # the catalog: WS/config (temp)
    monkeypatch.setenv("CAMERA_LAB_PAPERS_OUT", str(ws / "papers"))  # never the repo's papers
    locks = tmp_path / "locks"
    locks.mkdir(mode=0o700)
    monkeypatch.setenv("CAMERA_LAB_LOCK_DIR", str(locks))  # lock files stay in the test dir
    yield ws


@pytest.fixture(autouse=True)
def no_real_cameras(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-hw tests never load the Arena/eBUS drivers, even on the bench host."""
    if "hw" in request.keywords:
        return
    from camera_lab.two_d.service import TwoDService

    monkeypatch.setattr(TwoDService, "_load_real_drivers", lambda self: None)
