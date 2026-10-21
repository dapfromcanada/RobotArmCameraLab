"""Tests for core.presets: save/load/list, overwrite → .bak, path safety."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from camera_lab.core import paths
from camera_lab.core.config import ConfigError
from camera_lab.core.paths import UnsafePathError
from camera_lab.core.presets import (
    Preset,
    PresetExistsError,
    PresetNotFoundError,
    PresetStore,
)

CAMERA = {
    "vendor": "JAI Corporation",
    "model": "AP-3200T-PGE",
    "serial": "U330439",
    "firmware": "0.1.0.7",
}


class FakeNow:
    def __init__(self) -> None:
        self.t = datetime(2026, 10, 21, 12, 0, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        self.t += timedelta(seconds=1)
        return self.t


@pytest.fixture
def store(tmp_path: Path) -> PresetStore:
    return PresetStore(tmp_path / "presets", now=FakeNow())


def _preset(store: PresetStore, **kw: Any) -> Preset:
    fields: dict[str, Any] = {
        "name": "bin_white",
        "camera": CAMERA,
        "settings": {"exposure_us": 5000.0, "gain_db": 3.0, "pixel_format": "RGB8"},
        "lights": {"white_ring": 60.0},
        "tags": {"object": "bin", "lighting_setup": "ring", "notes": ["first try"]},
        "source_capture_id": "20261021-101500_U330439_run/0003",
        "scores_at_save": {"composite": 0.91},
        "created_by": "agent",
    }
    fields.update(kw)
    return store.new(**fields)


def test_save_load_round_trip(store: PresetStore) -> None:
    p = _preset(store)
    path = store.save(p)
    assert path == (store.root / "U330439" / "bin_white.yaml").resolve()
    data = yaml.safe_load(path.read_text())
    assert data["schema_version"] == 1
    assert data["camera"]["serial"] == "U330439"
    assert store.load("U330439", "bin_white") == p


def test_default_root(tmp_roots: Path) -> None:
    assert PresetStore().root == paths.two_d_presets_root()
    assert PresetStore().root == tmp_roots / "config" / "camera_presets" / "two_d"


def test_no_overwrite_without_flag(store: PresetStore) -> None:
    store.save(_preset(store))
    with pytest.raises(PresetExistsError, match="--force"):
        store.save(_preset(store, settings={"exposure_us": 1.0}))
    assert store.load("U330439", "bin_white").settings["exposure_us"] == 5000.0


def test_overwrite_keeps_bak(store: PresetStore) -> None:
    store.save(_preset(store))
    store.save(_preset(store, settings={"exposure_us": 1234.0}), overwrite=True)
    d = store.root / "U330439"
    baks = sorted(p.name for p in d.iterdir() if ".bak-" in p.name)
    assert len(baks) == 1
    assert baks[0].startswith("bin_white.yaml.bak-20261021T12")
    assert yaml.safe_load((d / baks[0]).read_text())["settings"]["exposure_us"] == 5000.0
    assert store.load("U330439", "bin_white").settings["exposure_us"] == 1234.0
    store.save(_preset(store), overwrite=True)
    assert len([p for p in d.iterdir() if ".bak-" in p.name]) == 2


def test_list(store: PresetStore) -> None:
    assert store.list() == []
    store.save(_preset(store, name="b"))
    store.save(_preset(store, name="a"))
    store.save(_preset(store, name="a"), overwrite=True)  # .bak must not be listed
    store.save(_preset(store, name="c", camera={**CAMERA, "serial": "233100505"}))
    (store.root / "junk dir").mkdir()
    (store.root / "U330439" / "Not Valid.yaml").write_text("x")
    assert [(r.serial, r.name) for r in store.list()] == [
        ("233100505", "c"),
        ("U330439", "a"),
        ("U330439", "b"),
    ]
    assert [r.name for r in store.list("233100505")] == ["c"]
    assert store.list("SIM-0001") == []


def test_load_missing(store: PresetStore) -> None:
    with pytest.raises(PresetNotFoundError):
        store.load("U330439", "nope")


def test_load_mismatched_file(store: PresetStore) -> None:
    path = store.save(_preset(store))
    other = path.with_name("renamed.yaml")
    path.rename(other)
    with pytest.raises(ConfigError, match="do not match"):
        store.load("U330439", "renamed")


def test_load_invalid_yaml_names_field(store: PresetStore) -> None:
    path = store.save(_preset(store))
    data = yaml.safe_load(path.read_text())
    data["lights"]["white_ring"] = 150
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ConfigError, match="lights"):
        store.load("U330439", "bin_white")


@pytest.mark.parametrize(
    ("serial", "name"),
    [
        ("../x", "a"),
        ("U330439", "../a"),
        ("U330439", "A"),
        ("a/b", "a"),
        ("U330439", "a.b"),
        ("", "a"),
    ],
)
def test_path_validation(store: PresetStore, serial: str, name: str) -> None:
    with pytest.raises(UnsafePathError):
        store.path(serial, name)


@pytest.mark.parametrize(
    "kw",
    [
        {"name": "../evil"},
        {"name": "Has Caps"},
        {"camera": {**CAMERA, "serial": "../x"}},
        {"lights": {"red": 120.0}},
        {"lights": {"red": float("nan")}},
        {"created_by": "root"},
        {"source_capture_id": "bad"},
        {"extra": 1},
        {"tags": {"notes": ["x" * 501]}},
    ],
)
def test_preset_model_validation(store: PresetStore, kw: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _preset(store, **kw)


def test_save_revalidates_mutated_model(store: PresetStore) -> None:
    p = _preset(store)
    object.__setattr__(p, "name", "../../escape")
    with pytest.raises(ValidationError):
        store.save(p)
