"""Tests for core.paths: safe_join and slug validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from camera_lab.core import paths
from camera_lab.core.paths import UnsafePathError, safe_join


@pytest.fixture
def base(tmp_path: Path) -> Path:
    b = tmp_path / "base"
    b.mkdir()
    return b


@pytest.mark.parametrize(
    "parts",
    [
        ("..",),
        ("a", ".."),
        ("..", "etc", "passwd"),
        ("/etc/passwd",),
        ("/",),
        ("a/../../b",),
        ("a/b",),
        ("a\\b",),
        (".",),
        ("",),
        ("a\x00b",),
        (".hidden",),
        ("-rf",),
        ("name with space",),
        ("naïve",),
        ("x" * 129,),
        ("run;rm",),
        ("$HOME",),
        ("~",),
    ],
)
def test_safe_join_rejects(base: Path, parts: tuple[str, ...]) -> None:
    with pytest.raises(UnsafePathError):
        safe_join(base, *parts)


def test_safe_join_rejects_no_parts(base: Path) -> None:
    with pytest.raises(UnsafePathError):
        safe_join(base)


def test_safe_join_rejects_non_str(base: Path) -> None:
    with pytest.raises(UnsafePathError):
        safe_join(base, 5)  # type: ignore[arg-type]


def test_safe_join_rejects_symlink_escape(base: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (base / "link").symlink_to(outside)
    with pytest.raises(UnsafePathError, match="escapes"):
        safe_join(base, "link", "file.txt")


@pytest.mark.parametrize(
    "parts",
    [
        ("20261021-101500_U330439_run",),
        ("SIM-0001", "daylight_1.yaml"),
        ("run", "captures", "0001.tiff"),
        ("best.yaml.bak-20261021T101500Z",),
    ],
)
def test_safe_join_accepts(base: Path, parts: tuple[str, ...]) -> None:
    result = safe_join(base, *parts)
    assert result == base.resolve().joinpath(*parts)
    assert result.is_relative_to(base.resolve())


@pytest.mark.parametrize("good", ["run", "a", "focus-test-2", "x" * 40])
def test_validate_slug_ok(good: str) -> None:
    assert paths.validate_slug(good) == good


@pytest.mark.parametrize("bad", ["", "Run", "a_b", "x" * 41, "a b", "../x", "a\n"])
def test_validate_slug_bad(bad: str) -> None:
    with pytest.raises(UnsafePathError):
        paths.validate_slug(bad)


def test_validate_preset_name() -> None:
    assert paths.validate_preset_name("bench_white-1") == "bench_white-1"
    for bad in ("", "Bench", "a.b", "x" * 41, "../a"):
        with pytest.raises(UnsafePathError):
            paths.validate_preset_name(bad)


def test_validate_serial() -> None:
    for good in ("233100505", "U330439", "SIM-0001"):
        assert paths.validate_serial(good) == good
    for bad in ("", "a_b", "a/b", "x" * 33, "..", "a b"):
        with pytest.raises(UnsafePathError):
            paths.validate_serial(bad)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Tune exposure for the bin!", "tune-exposure-for-the-bin"),
        ("   ", "run"),
        ("../../etc", "etc"),
        ("ÄÖÜ", "run"),
        ("a" * 60, "a" * 40),
    ],
)
def test_slugify(text: str, expected: str) -> None:
    assert paths.slugify(text) == expected
    paths.validate_slug(paths.slugify(text))
