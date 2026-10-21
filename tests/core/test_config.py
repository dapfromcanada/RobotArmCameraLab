"""Tests for core.config: safe YAML loading + pydantic validation."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import BaseModel, Field

from camera_lab.core import config
from camera_lab.core.config import ConfigError, load_yaml_model, read_yaml


class Inner(BaseModel):
    level: int = Field(ge=0, le=10)


class Model(BaseModel):
    name: str
    inner: Inner


def _write(tmp_path: Path, text: str, name: str = "c.yaml") -> Path:
    p = tmp_path / name
    p.write_text(text)
    return p


def test_load_ok(tmp_path: Path) -> None:
    p = _write(tmp_path, "name: a\ninner:\n  level: 3\n")
    m = load_yaml_model(p, Model)
    assert m == Model(name="a", inner=Inner(level=3))


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="file not found") as ei:
        load_yaml_model(tmp_path / "nope.yaml", Model)
    assert "nope.yaml" in str(ei.value)


def test_error_names_file_and_field(tmp_path: Path) -> None:
    p = _write(tmp_path, "name: a\ninner:\n  level: 99\n")
    with pytest.raises(ConfigError) as ei:
        load_yaml_model(p, Model)
    msg = str(ei.value)
    assert str(p) in msg
    assert "inner.level" in msg


def test_invalid_yaml_reports_line(tmp_path: Path) -> None:
    p = _write(tmp_path, "name: a\ninner: [unclosed\n")
    with pytest.raises(ConfigError, match="invalid YAML"):
        load_yaml_model(p, Model)


def test_python_tags_are_refused(tmp_path: Path) -> None:
    """safe_load must not construct arbitrary Python objects."""
    p = _write(
        tmp_path, "name: !!python/object/apply:os.system ['echo pwned']\ninner: {level: 1}\n"
    )
    with pytest.raises(ConfigError, match="invalid YAML"):
        read_yaml(p)


@pytest.mark.parametrize(("text", "match"), [("", "empty"), ("- a\n- b\n", "mapping")])
def test_empty_or_non_mapping(tmp_path: Path, text: str, match: str) -> None:
    with pytest.raises(ConfigError, match=match):
        load_yaml_model(_write(tmp_path, text), Model)


def test_too_large(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "MAX_YAML_BYTES", 10)
    with pytest.raises(ConfigError, match="limit"):
        read_yaml(_write(tmp_path, "name: abcdefghijkl\n"))


def test_directory_is_not_a_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="regular file"):
        read_yaml(tmp_path)


def test_bad_utf8(tmp_path: Path) -> None:
    p = tmp_path / "bad.yaml"
    p.write_bytes(b"name: \xff\xfe\n")
    with pytest.raises(ConfigError, match="cannot read"):
        read_yaml(p)
