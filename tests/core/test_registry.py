"""Tests for core.registry."""

from __future__ import annotations

import pytest

from camera_lab.core.registry import Registry, RegistryError


def test_register_get_names() -> None:
    reg: Registry[type] = Registry("thing")

    @reg.register("b")
    class B:
        pass

    @reg.register("a")
    class A:
        pass

    assert reg.get("a") is A
    assert reg.get("b") is B
    assert reg.names() == ["a", "b"]
    assert list(reg) == ["a", "b"]
    assert "a" in reg and "c" not in reg
    assert len(reg) == 2


def test_duplicate_raises() -> None:
    reg: Registry[int] = Registry("thing")
    reg.register("x")(1)
    with pytest.raises(RegistryError, match="duplicate thing name 'x'"):
        reg.register("x")(2)
    assert reg.get("x") == 1


def test_unknown_lists_known() -> None:
    reg: Registry[int] = Registry("metric")
    reg.register("mean")(1)
    with pytest.raises(RegistryError, match="unknown metric 'nope'; known: mean"):
        reg.get("nope")
    with pytest.raises(RegistryError, match="known: none"):
        Registry[int]("x").get("a")


@pytest.mark.parametrize("bad", ["", "Has Space", "UPPER", "a/b", "x" * 65])
def test_invalid_name(bad: str) -> None:
    with pytest.raises(RegistryError, match="invalid"):
        Registry[int]("thing").register(bad)
