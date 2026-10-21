"""Tests for the driver table, lazy loading and SDK import isolation."""

from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest

from camera_lab.core.registry import Registry, RegistryError
from camera_lab.two_d import drivers
from camera_lab.two_d.drivers import (
    VENDOR_DRIVERS,
    driver_for_vendor,
    load_driver_class,
)
from camera_lab.two_d.drivers.simulated import SimulatedCameraDriver
from camera_lab.two_d.errors import SdkUnavailableError


def test_vendor_table() -> None:
    assert driver_for_vendor("Lucid Vision Labs") == "arena"
    assert driver_for_vendor("JAI Corporation") == "ebus"
    assert driver_for_vendor("camera_lab") == "simulated"
    assert driver_for_vendor("Basler") is None
    with pytest.raises(TypeError):
        VENDOR_DRIVERS["Basler"] = "x"  # type: ignore[index]


def test_load_simulated() -> None:
    assert load_driver_class("simulated") is SimulatedCameraDriver


def test_unknown_driver() -> None:
    with pytest.raises(RegistryError):
        load_driver_class("bitflow")


def test_missing_module_is_sdk_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    reg: Registry[str] = Registry("camera driver")
    reg.register("fake")("camera_lab.two_d.drivers.does_not_exist:Nope")
    reg.register("wrong")("camera_lab.two_d.models:CameraInfo")
    monkeypatch.setattr(drivers, "DRIVER_MODULES", reg)
    with pytest.raises(SdkUnavailableError, match="unavailable") as ei:
        load_driver_class("fake")
    assert ei.value.exit_code == 5
    with pytest.raises(SdkUnavailableError, match="not a Camera2DDriver"):
        load_driver_class("wrong")


def test_importing_two_d_does_not_import_vendor_sdks() -> None:
    code = textwrap.dedent(
        """
        import sys
        import camera_lab.two_d
        import camera_lab.two_d.models, camera_lab.two_d.drivers
        import camera_lab.two_d.drivers.simulated, camera_lab.two_d.scoring
        import camera_lab.two_d.processing
        from camera_lab.two_d.drivers import load_driver_class
        load_driver_class("simulated")
        bad = sorted(m for m in sys.modules if m.split(".")[0] in
                     ("arena_api", "ebus_base", "ebus_edge", "ebus_receive"))
        print(",".join(bad))
        """
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=120, check=True
    )
    assert out.stdout.strip() == ""


def test_works_without_arena_sdk() -> None:
    """With arena_api unavailable, the driver module still imports and sim works."""
    code = textwrap.dedent(
        """
        import sys
        sys.modules["arena_api"] = None          # any import of arena_api now fails
        sys.modules["arena_api.system"] = None
        import camera_lab.two_d.drivers.arena as arena
        from camera_lab.two_d.drivers import load_driver_class
        from camera_lab.two_d.errors import SdkUnavailableError
        from camera_lab.two_d.service import build_service
        try:
            load_driver_class("arena")()
        except SdkUnavailableError as exc:
            print("sdk_unavailable", exc.exit_code)
        svc = build_service(sim=True)
        print(",".join(c.serial for c in svc.discover() if c.driver == "simulated"))
        svc.shutdown()
        """
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=120, check=True
    )
    assert out.stdout.split() == ["sdk_unavailable", "5", "SIM-0001"]
