"""A stand-in for the eBUS SDK (PvSystem / PvDevice / PvStream / PvPipeline / parameters).

Mirrors what the AP-3200T-PGE showed live (2026-10-21): node names and ranges,
gain as a multiplier with a selector, green gain read-only in master mode,
ExposureTime max following the frame period, nodes locked while streaming.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import numpy as np

from camera_lab.two_d.drivers.unpack import pack_rgb10p32, pack_rgb10v1, pack_rgb12v1

LOCKED_WHILE_STREAMING = {
    "PixelFormat",
    "Width",
    "Height",
    "OffsetX",
    "OffsetY",
    "TriggerMode",
    "ExposureMode",
}
BYTES_PER_PX = {"RGB8": 3.0, "RGB10V1Packed": 4.0, "RGB10p32": 4.0, "RGB12V1Packed": 4.5}


class R:
    """PvResult."""

    def __init__(self, code: str = "OK", desc: str = "") -> None:
        self.code, self.desc = code, desc

    def IsOK(self) -> bool:  # noqa: N802 - SDK naming
        return self.code == "OK"

    def GetCodeString(self) -> str:  # noqa: N802
        return self.code

    def HasDescription(self) -> bool:  # noqa: N802
        return bool(self.desc)

    def GetDescription(self) -> str:  # noqa: N802
        return self.desc


OK = R()


class _Entry:
    def __init__(self, name: str) -> None:
        self.name = name

    def IsAvailable(self) -> tuple[R, bool]:  # noqa: N802
        return OK, True

    def GetName(self) -> tuple[R, str]:  # noqa: N802
        return OK, self.name


#: Streamable features in node-map order, and what each selector selects (like the
#: live AP-3200T-PGE: ExposureTimeSelector → ExposureTime, GainSelector → Gain).
STREAMABLE = (
    "PixelFormat",
    "Width",
    "Height",
    "OffsetX",
    "OffsetY",
    "AcquisitionMode",
    "AcquisitionFrameRate",
    "TriggerSelector",
    "TriggerMode",
    "TriggerSource",
    "ExposureMode",
    "ExposureTimeMode",
    "ExposureTimeSelector",
    "ExposureTime",
    "ExposureAuto",
    "GainAuto",
    "BalanceWhiteAuto",
    "IndividualGainMode",
    "GainSelector",
    "Gain",
    "LUTMode",
    "Gamma",
    "TestPattern",
)
SELECTS = {
    "TriggerSelector": ("TriggerMode", "TriggerSource"),
    "ExposureTimeSelector": ("ExposureTime",),
    "GainSelector": ("Gain",),
}


class _List:
    """PvGenParameterList (GetFirst / GetNext)."""

    def __init__(self, items: list[Param]) -> None:
        self._items, self._i = items, 0

    def GetFirst(self) -> Param | None:  # noqa: N802
        self._i = 0
        return self.GetNext()

    def GetNext(self) -> Param | None:  # noqa: N802
        if self._i >= len(self._items):
            return None
        self._i += 1
        return self._items[self._i - 1]

    def GetSize(self) -> int:  # noqa: N802
        return len(self._items)


class Param:
    def __init__(self, cam: FakeCamera, name: str) -> None:
        self.cam, self.name = cam, name

    def GetName(self) -> tuple[R, str]:  # noqa: N802
        return OK, self.name

    def IsWritable(self) -> tuple[R, bool]:  # noqa: N802
        return OK, self.cam.writable(self.name)

    def IsReadable(self) -> tuple[R, bool]:  # noqa: N802
        return OK, True

    def IsStreamable(self) -> tuple[R, bool]:  # noqa: N802
        return OK, self.name in STREAMABLE

    def IsSelector(self) -> tuple[R, bool]:  # noqa: N802
        return OK, self.name in SELECTS

    def GetSelectedParameters(self) -> tuple[R, _List]:  # noqa: N802
        params = FakeParams(self.cam)
        return OK, _List(
            [q for n in SELECTS.get(self.name, ()) if (q := params.Get(n)) is not None]
        )

    def GetSelectingParameters(self) -> tuple[R, _List]:  # noqa: N802
        params = FakeParams(self.cam)
        sel = [params.Get(k) for k, v in SELECTS.items() if self.name in v]
        return OK, _List([q for q in sel if q is not None])

    def ToString(self) -> tuple[R, str]:  # noqa: N802
        return OK, str(self.cam.read(self.name))

    def FromString(self, text: str) -> R:  # noqa: N802
        cur = self.cam.read(self.name)
        value: Any = text
        if isinstance(cur, bool):
            value = text in ("1", "True", "true")
        elif isinstance(cur, int):
            value = int(text)
        elif isinstance(cur, float):
            value = float(text)
        return self.SetValue(value)

    def SetValue(self, v: Any) -> R:  # noqa: N802
        return self.cam.write(self.name, v)


class NumParam(Param):
    def GetValue(self) -> tuple[R, Any]:  # noqa: N802
        return OK, self.cam.read(self.name)

    def SetValue(self, v: Any) -> R:  # noqa: N802
        return self.cam.write(self.name, v)

    def GetMin(self) -> tuple[R, Any]:  # noqa: N802
        return OK, self.cam.lo(self.name)

    def GetMax(self) -> tuple[R, Any]:  # noqa: N802
        return OK, self.cam.hi(self.name)


class IntParam(NumParam):
    def GetIncrement(self) -> tuple[R, int]:  # noqa: N802
        return OK, self.cam.inc.get(self.name, 1)


class EnumParam(Param):
    def GetValueString(self) -> tuple[R, str]:  # noqa: N802
        return OK, self.cam.read(self.name)

    def SetValue(self, v: str) -> R:  # noqa: N802
        if v not in self.cam.entries[self.name]:
            return R("GENERIC_ERROR", f"GenApi exception on {self.name} SetValue {v}")
        return self.cam.write(self.name, v)

    def GetEntriesCount(self) -> tuple[R, int]:  # noqa: N802
        return OK, len(self.cam.entries[self.name])

    def GetEntryByIndex(self, i: int) -> tuple[R, _Entry]:  # noqa: N802
        return OK, _Entry(self.cam.entries[self.name][i])


#: Read-only identity strings (live AP-3200T-PGE 2026-10-22: discovery "version" is
#: DeviceVersion, the firmware is DeviceFirmwareVersion).
IDENTITY = {
    "DeviceModelName": "AP-3200T-PGE",
    "DeviceFirmwareVersion": "0.0.3.4",
    "DeviceVersion": "0.1.0.7",
}


class StrParam(Param):
    def GetValue(self) -> tuple[R, str]:  # noqa: N802
        return OK, IDENTITY[self.name]


class CmdParam(Param):
    def Execute(self) -> R:  # noqa: N802
        if self.name == "UserSetSave" and self.cam.streaming:  # live: refused while acquiring
            return R(
                "GENERIC_ERROR", "GenApi exception on UserSetSave Execute: Node is not writable."
            )
        self.cam.execute(self.name)
        return OK


class FakeCamera:
    """The device state behind the parameter array."""

    def __init__(self, *, lut: bool = True) -> None:
        self.v: dict[str, Any] = {
            "WidthMax": 2064,
            "HeightMax": 1544,
            "Width": 2064,
            "Height": 1544,
            "OffsetX": 0,
            "OffsetY": 0,
            "PixelFormat": "RGB8",
            "AcquisitionMode": "Continuous",
            "AcquisitionFrameRate": 5.0,
            "TriggerSelector": "AcquisitionStart",
            "ExposureMode": "Off",
            "ExposureTimeMode": "Common",
            "ExposureTimeSelector": "Common",
            "ExposureTime": 82402.0,
            "ExposureAuto": "Continuous",
            "GainAuto": "Off",
            "BalanceWhiteAuto": "Continuous",
            "IndividualGainMode": "Off",
            "GainSelector": "AnalogAll",
            "Gamma": 0.45,
            "LUTMode": "Off",
            "TestPattern": "GreyHorizontalRamp",
            "UserSetSelector": "UserSet1",
        }
        if not lut:
            del self.v["LUTMode"]
        self.trigger = {"AcquisitionStart": ("Off", "Low"), "FrameStart": ("On", "Line5")}
        self.gain = {
            "AnalogAll": 1.0,
            "AnalogRed": 1.0,
            "AnalogGreen": 1.0,
            "AnalogBlue": 1.0,
            "DigitalRed": 1.05,
            "DigitalBlue": 0.95,
        }
        self.gain_range = {
            "AnalogAll": (1.0, 8.0),
            "AnalogRed": (0.47, 4.0),
            "AnalogGreen": (1.0, 64.0),
            "AnalogBlue": (0.47, 4.0),
            "DigitalRed": (0.9, 1.1),
            "DigitalBlue": (0.9, 1.1),
        }
        self.entries = {
            "PixelFormat": ["RGB8", "RGB10V1Packed", "RGB10p32", "RGB12V1Packed"],
            "AcquisitionMode": ["SingleFrame", "MultiFrame", "Continuous"],
            "TriggerSelector": [
                "AcquisitionStart",
                "AcquisitionEnd",
                "FrameStart",
                "AcquisitionTransferStart",
            ],
            "TriggerMode": ["Off", "On"],
            "TriggerSource": ["Low", "High", "Software", "Line5", "Line6"],
            "ExposureMode": ["Off", "Timed", "TriggerWidth"],
            "ExposureTimeMode": ["Common", "Individual"],
            "ExposureTimeSelector": ["Common", "Red", "Green", "Blue"],
            "ExposureAuto": ["Off", "Once", "Continuous"],
            "GainAuto": ["Off", "Once", "Continuous"],
            "BalanceWhiteAuto": ["Off", "Once", "Continuous", "Preset5000K"],
            "IndividualGainMode": ["Off", "On"],
            "GainSelector": [
                "AnalogAll",
                "AnalogRed",
                "AnalogGreen",
                "AnalogBlue",
                "DigitalRed",
                "DigitalBlue",
            ],
            "LUTMode": ["Off", "Gamma", "LUT"],
            "TestPattern": ["Off", "GreyHorizontalRamp"],
            "UserSetSelector": ["Default", "UserSet1", "UserSet2", "UserSet3"],
        }
        self.inc = {"Width": 16, "OffsetX": 16, "Height": 2, "OffsetY": 2}
        self.channel_exposure = {"Red": 20164.0, "Green": 20164.0, "Blue": 20164.0}
        self.streaming = False
        self.connected = True
        self.block_id = 0
        self.queue: list[Any] = []
        self.pending_triggers = 0
        self.fault: str | None = None
        self.saved_user_sets: dict[str, dict[str, Any]] = {}

    # ---- geometry-dependent limits
    def fps_max(self) -> float:
        px = self.v["Width"] * self.v["Height"] * BYTES_PER_PX[self.v["PixelFormat"]]
        return min(60.0, 12.0279 * (2064 * 1544 * 3.0) / px)

    def lo(self, name: str) -> Any:
        if name == "Gain":
            return self.gain_range[self.v["GainSelector"]][0]
        return {
            "Width": 16,
            "Height": 2,
            "OffsetX": 0,
            "OffsetY": 0,
            "ExposureTime": 1.0,
            "AcquisitionFrameRate": 0.125,
            "Gamma": 0.45,
        }.get(name)

    def hi(self, name: str) -> Any:
        v = self.v
        if name == "Gain":
            if v["IndividualGainMode"] == "On" and v["GainSelector"].startswith("Analog"):
                return 64.0  # live: x1..x64 per sensor in individual gain mode
            return self.gain_range[v["GainSelector"]][1]
        return {
            "Width": v["WidthMax"] - v["OffsetX"],
            "Height": v["HeightMax"] - v["OffsetY"],
            "OffsetX": v["WidthMax"] - v["Width"],
            "OffsetY": v["HeightMax"] - v["Height"],
            # live: the readout margin depends on the format (RGB8 737 µs, RGB12V1Packed
            # 1107 µs, 2026-10-24)
            "ExposureTime": 1e6 / v["AcquisitionFrameRate"]
            - (1107.0 if v.get("PixelFormat") == "RGB12V1Packed" else 737.0),
            "AcquisitionFrameRate": self.fps_max(),
            "Gamma": 1.0,
        }.get(name)

    # ---- access
    def writable(self, name: str) -> bool:
        if self.streaming and name in LOCKED_WHILE_STREAMING:
            return False
        if name in ("WidthMax", "HeightMax"):
            return False
        individual_gain = self.v["IndividualGainMode"] == "On"
        if name == "Gain":
            sel = self.v["GainSelector"]
            if sel == "AnalogGreen" and not individual_gain:
                return False
            if sel == "AnalogAll" and individual_gain:
                return False
        if name == "ExposureTime":  # live: Common xor per-sensor, by ExposureTimeMode
            common_selected = self.v["ExposureTimeSelector"] == "Common"
            return common_selected == (self.v["ExposureTimeMode"] == "Common")
        return not (name == "ExposureTimeMode" and self.streaming)

    def read(self, name: str) -> Any:
        if not self.connected:
            raise RuntimeError("not connected")
        if name == "Gain":
            return self.gain[self.v["GainSelector"]]
        if name == "ExposureTime" and self.v["ExposureTimeSelector"] != "Common":
            return self.channel_exposure[self.v["ExposureTimeSelector"]]
        if name in ("TriggerMode", "TriggerSource"):
            mode, src = self.trigger.get(self.v["TriggerSelector"], ("Off", "Low"))
            return mode if name == "TriggerMode" else src
        return self.v[name]

    def write(self, name: str, value: Any) -> R:
        if not self.writable(name):
            return R("GENERIC_ERROR", f"{name} is not writable")
        lo, hi = self.lo(name), self.hi(name)
        if isinstance(value, int | float) and not isinstance(value, bool):
            if (lo is not None and value < lo - 1e-9) or (hi is not None and value > hi + 1e-9):
                return R("GENERIC_ERROR", f"{name} {value} out of range")
            step = self.inc.get(name)
            if step and int(value) % step:
                return R("GENERIC_ERROR", f"{name} {value} not on increment {step}")
        if name == "Gain":
            sel = self.v["GainSelector"]
            q = 64.0 if sel in ("AnalogRed", "AnalogBlue") else 1000.0  # WB gains quantise to 1/64
            self.gain[sel] = round(float(value) * q) / q
            return OK
        if name in ("TriggerMode", "TriggerSource"):
            mode, src = self.trigger.get(self.v["TriggerSelector"], ("Off", "Low"))
            self.trigger[self.v["TriggerSelector"]] = (
                (value, src) if name == "TriggerMode" else (mode, value)
            )
            return OK
        if name == "ExposureTime" and self.v["ExposureTimeSelector"] != "Common":
            self.channel_exposure[self.v["ExposureTimeSelector"]] = float(value)
            return OK
        self.v[name] = value
        if name in ("Width", "Height", "PixelFormat"):
            self.v["AcquisitionFrameRate"] = min(self.v["AcquisitionFrameRate"], self.fps_max())
        if name == "AcquisitionFrameRate":
            self.v["ExposureTime"] = min(self.v["ExposureTime"], self.hi("ExposureTime"))
        return OK

    def execute(self, name: str) -> None:
        if name == "UserSetSave":
            self.saved_user_sets[self.v["UserSetSelector"]] = {
                "ExposureTime": self.v["ExposureTime"]
            }
        if name == "AcquisitionStart":
            self.streaming = True
        elif name == "AcquisitionStop":
            self.streaming = False
        elif name == "TriggerSoftware":
            self.pending_triggers += 1

    # ---- images
    def image(self, block: int) -> np.ndarray:
        w, h = self.v["Width"], self.v["Height"]
        bits = {"RGB8": 8, "RGB12V1Packed": 12}.get(self.v["PixelFormat"], 10)
        return ((np.arange(w * h * 3, dtype=np.uint32) * 5 + block) % (1 << bits)).reshape(h, w, 3)

    def payload(self, img: np.ndarray) -> np.ndarray:
        fmt = self.v["PixelFormat"]
        if fmt == "RGB8":
            return img.astype(np.uint8).reshape(-1)
        return {
            "RGB10p32": pack_rgb10p32,
            "RGB10V1Packed": pack_rgb10v1,
            "RGB12V1Packed": pack_rgb12v1,
        }[fmt](img)


class FakeParams:
    def __init__(self, cam: FakeCamera) -> None:
        self.cam = cam

    def _names(self) -> list[str]:
        cmds = ["AcquisitionStart", "AcquisitionStop", "TriggerSoftware"]
        return [*STREAMABLE, "WidthMax", "HeightMax", *cmds]

    def GetCount(self) -> int:  # noqa: N802
        return len(self._names())

    def Get(self, name: str | int) -> Param | None:  # noqa: N802
        if isinstance(name, int):
            return self.Get(self._names()[name])
        cam = self.cam
        if name in (
            "AcquisitionStart",
            "AcquisitionStop",
            "TriggerSoftware",
            "UserSetSave",
            "UserSetLoad",
        ):
            return CmdParam(cam, name)
        if name in cam.entries and (name in cam.v or name in ("TriggerMode", "TriggerSource")):
            return EnumParam(cam, name)
        if name in ("Width", "Height", "OffsetX", "OffsetY", "WidthMax", "HeightMax"):
            return IntParam(cam, name)
        if name in ("ExposureTime", "AcquisitionFrameRate", "Gain", "Gamma"):
            return NumParam(cam, name)
        if name in IDENTITY:
            return StrParam(cam, name)
        return None


class FakeInfo:
    def __init__(self, vendor: str, serial: str, model: str, version: str, ip: str) -> None:
        self.vendor, self.serial, self.model, self.version, self.ip = (
            vendor,
            serial,
            model,
            version,
            ip,
        )

    def GetVendorName(self) -> str:  # noqa: N802
        return self.vendor

    def GetSerialNumber(self) -> str:  # noqa: N802
        return self.serial

    def GetModelName(self) -> str:  # noqa: N802
        return self.model

    def GetVersion(self) -> str:  # noqa: N802
        return self.version

    def GetIPAddress(self) -> str:  # noqa: N802
        return self.ip

    def GetConnectionID(self) -> str:  # noqa: N802
        return self.ip


class FakeDevice:
    def __init__(self, sdk: FakeSdk, info: FakeInfo) -> None:
        self.sdk, self.info = sdk, info
        self.cam = FakeCamera(**sdk.cam_kw)
        self.packet = 1476
        self.freed = False

    def GetParameters(self) -> FakeParams:  # noqa: N802
        return FakeParams(self.cam)

    def IsConnected(self) -> bool:  # noqa: N802
        return self.cam.connected

    def NegotiatePacketSize(self) -> R:  # noqa: N802
        self.packet = 8944
        return OK

    def SetPacketSize(self, size: int) -> R:  # noqa: N802
        self.packet = size
        return OK

    def SetStreamDestination(self, ip: str, port: int) -> R:  # noqa: N802
        return OK

    def GetPayloadSize(self) -> int:  # noqa: N802
        v = self.cam.v
        return int(v["Width"] * v["Height"] * BYTES_PER_PX[v["PixelFormat"]])

    def StreamEnable(self) -> R:  # noqa: N802
        return OK

    def StreamDisable(self) -> R:  # noqa: N802
        return OK

    def Disconnect(self) -> R:  # noqa: N802
        self.sdk.disconnected.append(self)
        return OK


class FakeBuffer:
    def __init__(self, data: np.ndarray, w: int, h: int, block: int) -> None:
        self.data, self.w, self.h, self.block = data, w, h, block

    def GetImage(self) -> Any:  # noqa: N802
        return SimpleNamespace(GetWidth=lambda: self.w, GetHeight=lambda: self.h)

    def GetDataPointer(self) -> np.ndarray:  # noqa: N802
        return self.data

    def GetBlockID(self) -> int:  # noqa: N802
        return self.block


class FakePipeline:
    def __init__(self, stream: FakeStream) -> None:
        self.stream = stream
        self.dev = stream.dev
        self.started = False
        self.outstanding: list[FakeBuffer] = []
        self.backlog = 0  # frames already waiting in the output queue

    def SetBufferCount(self, n: int) -> None:  # noqa: N802
        self.count = n

    def SetBufferSize(self, n: int) -> None:  # noqa: N802
        self.size = n

    def Start(self) -> R:  # noqa: N802
        self.started = True
        self.stream.pipelines.append(self)
        return OK

    def Stop(self) -> R:  # noqa: N802
        self.started = False
        return OK

    def GetOutputQueueSize(self) -> int:  # noqa: N802
        return self.backlog

    def RetrieveNextBuffer(self, timeout: int) -> tuple[R, Any, Any]:  # noqa: N802
        cam = self.dev.cam
        if not cam.connected:
            return R("TIMEOUT"), None, None
        if self.backlog:
            self.backlog -= 1
        elif not cam.streaming or cam.fault == "timeout":
            cam.fault = None
            return R("TIMEOUT"), None, None
        elif cam.trigger["FrameStart"][0] == "On":
            if not cam.pending_triggers:
                return R("TIMEOUT"), None, None
            cam.pending_triggers -= 1
        cam.block_id += 1
        img = cam.image(cam.block_id)
        buf = FakeBuffer(cam.payload(img), cam.v["Width"], cam.v["Height"], cam.block_id)
        self.outstanding.append(buf)
        op = R("MISSING_PACKETS") if cam.fault == "incomplete" else OK
        if cam.fault == "incomplete":
            cam.fault = None
        return OK, buf, op

    def ReleaseBuffer(self, buf: FakeBuffer) -> R:  # noqa: N802
        self.outstanding.remove(buf)
        return OK


class FakeStream:
    def __init__(self, dev: FakeDevice) -> None:
        self.dev = dev
        self.pipelines: list[FakePipeline] = []
        self.closed = False

    def GetLocalIPAddress(self) -> str:  # noqa: N802
        return "192.168.10.1"

    def GetLocalPort(self) -> int:  # noqa: N802
        return 50000

    def Close(self) -> R:  # noqa: N802
        self.closed = True
        return OK


class FakeSdk:
    """Bundles the fake SDK classes the driver uses (``sdk.PvSystem`` …)."""

    def __init__(self, *, busy: bool = False, **cam_kw: Any) -> None:
        self.infos = [
            FakeInfo("Lucid Vision Labs", "233100505", "TRI054S-C", "1.138.0.0", "192.168.20.229"),
            FakeInfo("JAI Corporation", "U330439", "AP-3200T-PGE", "0.1.0.7", "192.168.10.44"),
        ]
        self.busy = busy
        self.cam_kw = cam_kw
        self.connected: list[FakeDevice] = []
        self.disconnected: list[FakeDevice] = []
        self.freed: list[Any] = []
        self.streams: list[FakeStream] = []
        self.detection_timeout: int | None = None
        sdk = self

        class PvSystem:
            def SetDetectionTimeout(self, ms: int) -> None:  # noqa: N802
                sdk.detection_timeout = ms

            def Find(self) -> R:  # noqa: N802
                return OK

            def GetDeviceCount(self) -> int:  # noqa: N802
                return len(sdk.infos)

            def GetDeviceInfo(self, i: int) -> FakeInfo:  # noqa: N802
                return sdk.infos[i]

        class PvDevice:
            @staticmethod
            def CreateAndConnect(info: FakeInfo) -> tuple[R, Any]:  # noqa: N802
                if info.vendor != "JAI Corporation":
                    raise AssertionError("eBUS driver must never connect to a non-JAI camera")
                if sdk.busy:
                    return R("GENERIC_ERROR"), None
                dev = FakeDevice(sdk, info)
                sdk.connected.append(dev)
                return OK, dev

            @staticmethod
            def Free(dev: Any) -> None:  # noqa: N802
                sdk.freed.append(dev)

        class PvStream:
            @staticmethod
            def CreateAndOpen(conn: str) -> tuple[R, Any]:  # noqa: N802
                st = FakeStream(sdk.connected[-1])
                sdk.streams.append(st)
                return OK, st

            @staticmethod
            def Free(st: Any) -> None:  # noqa: N802
                sdk.freed.append(st)

        self.PvSystem = PvSystem
        self.PvDevice = PvDevice
        self.PvStream = PvStream
        self.PvPipeline = FakePipeline
