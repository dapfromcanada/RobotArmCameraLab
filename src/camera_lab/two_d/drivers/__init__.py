"""Camera drivers: the vendor → driver table and a lazy-import registry.

Drivers are chosen by **vendor name**. Lucid cameras go only through Arena and
JAI cameras only through eBUS (both SDKs can see both cameras, so each driver
must skip the other vendor's devices). Vendor SDK modules are imported only
when the driver is loaded, so ``import camera_lab.two_d`` never imports
``arena_api`` or ``ebus_*``. A failing import raises
:class:`~camera_lab.two_d.errors.SdkUnavailableError` (CLI exit code 5).
"""

from __future__ import annotations

import importlib
from types import MappingProxyType

from camera_lab.core.registry import Registry
from camera_lab.two_d.drivers.base import Camera2D, Camera2DDriver
from camera_lab.two_d.errors import SdkUnavailableError

#: The one table mapping a GenICam vendor name to a driver name.
VENDOR_DRIVERS: MappingProxyType[str, str] = MappingProxyType(
    {
        "Lucid Vision Labs": "arena",
        "JAI Corporation": "ebus",
        "camera_lab": "simulated",
    }
)

#: Driver name → ``module:ClassName``; imported on first use.
DRIVER_MODULES: Registry[str] = Registry("camera driver")
DRIVER_MODULES.register("simulated")("camera_lab.two_d.drivers.simulated:SimulatedCameraDriver")
DRIVER_MODULES.register("arena")("camera_lab.two_d.drivers.arena:ArenaDriver")
DRIVER_MODULES.register("ebus")("camera_lab.two_d.drivers.ebus:EbusDriver")

__all__ = [
    "DRIVER_MODULES",
    "VENDOR_DRIVERS",
    "Camera2D",
    "Camera2DDriver",
    "driver_for_vendor",
    "load_driver_class",
]


_VENDOR_KEYS = {k.casefold(): v for k, v in VENDOR_DRIVERS.items()}


def driver_for_vendor(vendor: str) -> str | None:
    """Return the driver name for a vendor (case-insensitive), or ``None``.

    Case-insensitive because the Arena docs spell the vendor "LUCID Vision Labs"
    while the TRI054S-C reports "Lucid Vision Labs".
    """
    return _VENDOR_KEYS.get(vendor.strip().casefold())


def load_driver_class(name: str) -> type[Camera2DDriver]:
    """Import and return the driver class registered as ``name``.

    Raises:
        SdkUnavailableError: the driver module or its vendor SDK cannot be imported.
        RegistryError: ``name`` is not a known driver.
    """
    target = DRIVER_MODULES.get(name)
    module_name, _, attr = target.partition(":")
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise SdkUnavailableError(f"driver {name!r} unavailable: {exc}") from exc
    cls = getattr(module, attr, None)
    if not (isinstance(cls, type) and issubclass(cls, Camera2DDriver)):
        raise SdkUnavailableError(f"driver {name!r}: {target} is not a Camera2DDriver")
    return cls
