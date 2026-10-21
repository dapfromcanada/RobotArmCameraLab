"""A dict-backed stand-in for the arena_api system/device/node API.

Mirrors what the TRI054S-C showed live (2026-10-21): node names, ranges and
increments, auto white balance making BalanceRatio read-only, transport-layer
parameters locked while streaming, frame rate writable only when enabled.
"""

from __future__ import annotations

import ctypes
from pathlib import Path
from typing import Any, ClassVar

import numpy as np

from camera_lab.two_d.drivers.unpack import pack_12p

LUCID_INFO = {
    "model": "TRI054S-C",
    "vendor": "Lucid Vision Labs",
    "serial": "233100505",
    "ip": "192.168.20.229",
    "version": "1.138.0.0",
    "mac": "1c:0f:af:0c:ac:1f",
}
JAI_INFO = {
    "model": "AP-3200T-PGE",
    "vendor": "JAI Corporation",
    "serial": "U330439",
    "ip": "192.168.10.44",
    "version": "0.1.0.7",
}
FORMATS = [
    "Mono8",
    "Mono12p",
    "Mono16",
    "Mono24",
    "BayerRG8",
    "BayerRG12p",
    "BayerRG16",
    "RGB8",
    "RGB12p",
    "RGB16",
    "BGR8",
    "YCbCr422_8_CbYCrY",
]
LOCKED_WHILE_STREAMING = {
    "PixelFormat",
    "Width",
    "Height",
    "OffsetX",
    "OffsetY",
    "AcquisitionFrameRateEnable",  # live 2026-10-21: frame rate is locked while streaming
    "AcquisitionFrameRate",
}


class FakeNode:
    def __init__(
        self,
        owner: FakeNodeMap,
        name: str,
        value: Any = None,
        *,
        lo: Any = None,
        hi: Any = None,
        inc: Any = None,
        entries: list[str] | None = None,
        writable: bool = True,
        readable: bool = True,
    ) -> None:
        self._owner = owner
        self.name = name
        self._value = value
        self._min, self._max, self._inc = lo, hi, inc
        self.enumentry_names = entries
        self._writable = writable
        self._readable = readable
        self.executed = 0

    @property
    def is_readable(self) -> bool:
        return self._readable

    @property
    def is_writable(self) -> bool:
        return self._owner.writable(self)

    @property
    def min(self) -> Any:
        return self._owner.dyn_min(self)

    @property
    def max(self) -> Any:
        return self._owner.dyn_max(self)

    @property
    def inc(self) -> Any:
        return self._inc

    @property
    def value(self) -> Any:
        if self._owner.device.disconnected:
            raise RuntimeError("Arena ERROR: device not connected")
        if not self._readable:
            raise RuntimeError(f"Arena ERROR: Node is not readable: {self.name}")
        return self._owner.read(self)

    @value.setter
    def value(self, v: Any) -> None:
        if self._owner.device.disconnected:
            raise RuntimeError("Arena ERROR: device not connected")
        if not self.is_writable:
            raise RuntimeError(f"Arena ERROR: {self.name} is not writable")
        if self.enumentry_names is not None and v not in self.enumentry_names:
            raise ValueError(f"'{v}' is not a valid entry for {self.name}")
        lo, hi = self.min, self.max
        if (lo is not None and v < lo) or (hi is not None and v > hi):
            raise ValueError(f"Arena ERROR: {self.name} value {v} out of range")
        if isinstance(self._inc, int) and self._inc > 1 and (v - (lo or 0)) % self._inc:
            raise ValueError(f"Arena ERROR: {self.name} value {v} not on increment")
        self._owner.write(self, v)

    def execute(self) -> None:
        self.executed += 1
        self._owner.device.on_execute(self.name)


class FakeNodeMap:
    def __init__(
        self,
        device: FakeDevice,
        *,
        gamma: bool = False,
        colour: bool = True,
        hdr: bool = True,
        locked_wb: bool = False,
    ) -> None:
        self.device = device
        self.wb = {"Red": 1.0, "Green": 1.0, "Blue": 1.0}
        n = self._add
        n("ExposureAuto", "Continuous", entries=["Off", "Once", "Continuous"])
        n("ExposureTime", 29991.416, lo=86.264, hi=None)  # max follows the frame period
        n("HDRTuningEnable", False)
        if hdr:
            n(
                "HDROutput",
                "HDR",
                entries=["HDR", "HDRChannel0", "HDRChannel1", "HDRChannel2", "HDRChannel3"],
            )
            n("HDRTuningChannelSelector", 0, lo=0, hi=3, inc=1)
            n("HDRChannelExposureTime", None, lo=86.264, hi=None)
            n("HDRChannelAnalogGain", None, lo=0.0, hi=None)
            n("HDRImageEnhancementEnable", True)
            n("HDRDigitalClampingEnable", True)
        self.hdr_exposure = [10000.472] * 4
        self.hdr_gain = [19.8, 4.2, 29.4, 4.2]
        self.hdr_gain_max = [30.0, 14.4, 38.4, 18.0]  # live ranges per channel
        n("GainAuto", "Off", entries=["Off", "Once", "Continuous"])
        n("Gain", 0.0, lo=0.0, hi=42.0)
        if gamma:
            n("Gamma", 1.0, lo=0.2, hi=2.0)
            n("GammaEnable", False)
        if colour:
            n("BalanceWhiteAuto", "Continuous", entries=["Off", "Once", "Continuous"])
            n("BalanceWhiteEnable", True)
            n("BalanceRatioSelector", "Red", entries=["Red", "Green", "Blue"])
            n("BalanceRatio", None, lo=0.0, hi=256.0)
        elif locked_wb:  # TRI089S-M: WB nodes listed but not readable or writable (live)
            for name in ("BalanceWhiteAuto", "BalanceRatioSelector", "BalanceRatio"):
                n(name, None, writable=False, readable=False)
        n("PixelFormat", "RGB8", entries=FORMATS if colour else ["Mono8", "Mono12p", "Mono16"])
        n("WidthMax", 2880, writable=False)
        n("HeightMax", 1860, writable=False)
        n("Width", 2880, lo=4, inc=4)
        n("Height", 1860, lo=2, inc=2)
        n("OffsetX", 0, lo=0, inc=4)
        n("OffsetY", 0, lo=0, inc=2)
        n("AcquisitionMode", "Continuous", entries=["Continuous", "SingleFrame", "MultiFrame"])
        n("AcquisitionFrameRateEnable", False)
        n("AcquisitionFrameRate", 6.558, lo=0.2, hi=6.558)
        n("TriggerSelector", "FrameStart", entries=["AcquisitionStart", "FrameStart"])
        n("TriggerMode", "Off", entries=["Off", "On"])
        n("TriggerSource", "Line0", entries=["Software", "Line0", "Line2"])
        n("TriggerArmed", False, writable=False)
        n("TriggerSoftware", None)
        n("TimestampLatch", None)
        # user sets (live TRI054S-C): UserSetSave is not writable while Default is selected;
        # UserSetFeatureSelector lists what a user set stores (no HDR tuning / channels)
        n("UserSetSelector", "Default", entries=["Default", "UserSet1", "UserSet2"])
        n("UserSetSave", None)
        n("UserSetLoad", None)
        n("UserSetDefault", "Default", entries=["Default", "UserSet1", "UserSet2"])
        n(
            "UserSetFeatureSelector",
            "ExposureTime",
            entries=["ExposureTime", "Gain", "BalanceRatio", "PixelFormat", "HDROutput"],
        )
        n("TimestampLatchValue", 0, writable=False)

    def _add(self, name: str, value: Any, **kw: Any) -> None:
        setattr(self, "_n_" + name, FakeNode(self, name, value, **kw))

    def get_node(self, name: str) -> FakeNode | None:
        return getattr(self, "_n_" + name, None)

    def __getitem__(self, name: str) -> FakeNode:
        node = self.get_node(name)
        if node is None:
            raise KeyError(name)
        return node

    # GenApi feature streaming (live TRI054S-C: HDR nodes are not streamable;
    # features that are read-only at save time are left out)
    STREAMABLE: ClassVar[tuple[str, ...]] = (
        "PixelFormat",
        "Width",
        "Height",
        "OffsetX",
        "OffsetY",
        "AcquisitionMode",
        "AcquisitionFrameRateEnable",
        "AcquisitionFrameRate",
        "ExposureAuto",
        "ExposureTime",
        "GainAuto",
        "Gain",
        "TriggerSelector",
        "TriggerMode",
        "TriggerSource",
        "BalanceRatioSelector",  # live file order: ratios, then BalanceWhiteAuto
        "BalanceWhiteAuto",
        "BalanceWhiteEnable",
    )

    @staticmethod
    def _fmt(v: Any) -> str:
        return str(int(v)) if isinstance(v, bool) else str(v)

    def write_streamable_node_values_to(self, file_name: str) -> None:
        if self.device.streaming:  # live: DeviceFeaturePersistenceStart locked while streaming
            raise RuntimeError("Arena ERROR: DeviceFeaturePersistenceStart is not writable")
        lines = [
            "# {05D8C294-F295-4dfb-9D01-096BD04049F4}",
            "# GenApi persistence file (version 3.3.0)",
        ]
        for name in self.STREAMABLE:
            node = self.get_node(name)
            if node is None or not node.is_readable or not node.is_writable:
                continue
            if name == "BalanceRatioSelector" and self["BalanceRatio"].is_writable:
                prev = node._value
                for entry in node.enumentry_names or []:
                    node._value = entry
                    lines += [
                        f"{name}\t{entry}",
                        f"BalanceRatio\t{self._fmt(self['BalanceRatio'].value)}",
                    ]
                node._value = prev
            lines.append(f"{name}\t{self._fmt(node.value)}")
        Path(file_name).write_text("\n".join(lines) + "\n")

    def read_streamable_node_values_from(self, file_name: str) -> None:
        for line in Path(file_name).read_text().splitlines():
            if not line or line.startswith("#"):
                continue
            name, text = line.split("\t", 1)
            node = self[name]
            cur = node._value
            value: Any = (
                text == "1"
                if isinstance(cur, bool)
                else float(text)
                if isinstance(cur, float) or (cur is None and "." in text)
                else int(text)
                if isinstance(cur, int)
                else text
            )
            node.value = value

    # dynamic behaviour ---------------------------------------------------
    def writable(self, node: FakeNode) -> bool:
        if not node._writable:
            return False
        if node.name == "UserSetSave":
            return self["UserSetSelector"]._value != "Default"
        if self.device.streaming and node.name in LOCKED_WHILE_STREAMING:
            return False
        if node.name == "BalanceRatio":
            return self["BalanceWhiteAuto"]._value == "Off"
        if node.name == "AcquisitionFrameRate":
            return bool(self["AcquisitionFrameRateEnable"]._value)
        if node.name in ("ExposureTime", "ExposureAuto", "Gain", "GainAuto"):
            return not self["HDRTuningEnable"]._value
        return True

    def dyn_min(self, node: FakeNode) -> Any:
        return node._min

    def frame_rate(self) -> float:
        rate = self["AcquisitionFrameRate"]
        return float(rate._value if self["AcquisitionFrameRateEnable"]._value else rate._max)

    def dyn_max(self, node: FakeNode) -> Any:
        if node.name == "HDRChannelAnalogGain":
            return self.hdr_gain_max[self["HDRTuningChannelSelector"]._value]
        if node.name in ("ExposureTime", "HDRChannelExposureTime"):  # live: ≈ frame period − 167 µs
            return 1e6 / self.frame_rate() - 167.0
        if node.name == "OffsetX":
            return self["WidthMax"]._value - self["Width"]._value
        if node.name == "OffsetY":
            return self["HeightMax"]._value - self["Height"]._value
        if node.name == "Width":
            return self["WidthMax"]._value - self["OffsetX"]._value
        if node.name == "Height":
            return self["HeightMax"]._value - self["OffsetY"]._value
        return node._max

    def read(self, node: FakeNode) -> Any:
        if node.name == "BalanceRatio":
            return self.wb[self["BalanceRatioSelector"]._value]
        if node.name == "TriggerArmed":
            return self.device.streaming and self["TriggerMode"]._value == "On"
        if node.name == "HDRChannelExposureTime":
            return self.hdr_exposure[self["HDRTuningChannelSelector"]._value]
        if node.name == "HDRChannelAnalogGain":
            return self.hdr_gain[self["HDRTuningChannelSelector"]._value]
        return node._value

    def write(self, node: FakeNode, v: Any) -> None:
        if node.name == "BalanceRatio":
            # the camera quantises ratios to 1/1024
            self.wb[self["BalanceRatioSelector"]._value] = round(v * 1024) / 1024
        elif node.name == "HDRChannelExposureTime":
            self.hdr_exposure[self["HDRTuningChannelSelector"]._value] = float(v)
        elif node.name == "HDRChannelAnalogGain":
            self.hdr_gain[self["HDRTuningChannelSelector"]._value] = round(float(v), 1)
        else:
            node._value = v


class FakeBuffer:
    def __init__(
        self,
        data: bytes,
        width: int,
        height: int,
        frame_id: int,
        incomplete: bool,
        timestamp_ns: int = 0,
    ) -> None:
        self._arr = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
        self.pdata = ctypes.cast(self._arr, ctypes.POINTER(ctypes.c_ubyte))
        self.payload_size = len(data)
        self.width, self.height = width, height
        self.frame_id = frame_id
        self.is_incomplete = incomplete
        self.timestamp_ns = timestamp_ns  # device clock at frame start


def expected_image(fmt: str, width: int, height: int, level: int) -> np.ndarray:
    """The deterministic image the fake device sends (a ramp + constant level)."""
    ch = 3 if fmt.startswith("RGB") else 1
    bits = 8 if fmt.endswith("8") else 12 if fmt.endswith("12p") else 16
    ramp = (np.arange(width * height * ch, dtype=np.uint32) * 7 + level) % (1 << bits)
    shape = (height, width, 3) if ch == 3 else (height, width)
    return ramp.astype(np.uint8 if bits == 8 else np.uint16).reshape(shape)


class FakeDevice:
    def __init__(self, info: dict[str, Any], **nm_kw: Any) -> None:
        self.info = info
        self.streaming = False
        self.disconnected = False
        self.nodemap = FakeNodeMap(self, **nm_kw)
        self.tl_stream_nodemap = _StreamNodeMap(self)
        self.frame_id = 0
        self.outstanding: list[FakeBuffer] = []
        self.requeued = 0
        self.fault: str | None = None
        self.pending_triggers = 0
        self.clock_ns = 1_000_000_000  # device timestamp counter (ns)
        self.in_flight = 0  # frames already exposing when a setting is written
        self.saved_user_sets: dict[str, dict[str, Any]] = {}
        self.start_count = 0

    def is_connected(self) -> bool:
        return not self.disconnected

    def start_stream(self, n: int | None = None) -> None:
        if self.disconnected:
            raise RuntimeError("Arena ERROR: not connected")
        self.streaming = True
        self.start_count += 1

    def stop_stream(self) -> None:
        self.streaming = False

    def on_execute(self, name: str) -> None:
        if name == "UserSetSave":
            if not self.nodemap.writable(self.nodemap["UserSetSave"]):
                raise RuntimeError("Arena ERROR: UserSetSave is not writable")
            self.saved_user_sets[self.nodemap["UserSetSelector"]._value] = {
                "ExposureTime": self.nodemap["ExposureTime"]._value
            }
        elif name == "TriggerSoftware":
            self.pending_triggers += 1
        elif name == "TimestampLatch":
            # frames already in flight (``in_flight``) were stamped before the latch
            period = int(1e9 / self.nodemap.frame_rate())
            self.nodemap["TimestampLatchValue"]._value = self.clock_ns + self.in_flight * period

    def get_buffer(self, timeout: int | None = None) -> FakeBuffer:
        if not self.streaming:
            raise BaseException("get_buffer before start_stream")
        if self.disconnected or self.fault == "timeout":
            self.fault = None
            raise TimeoutError("timeout")
        nm = self.nodemap
        if nm["TriggerMode"]._value == "On":
            if not self.pending_triggers:
                raise TimeoutError("no trigger")
            self.pending_triggers -= 1
        self.frame_id += 1
        stamp = self.clock_ns
        self.clock_ns += int(1e9 / nm.frame_rate())
        fmt = nm["PixelFormat"]._value
        w, h = nm["Width"]._value, nm["Height"]._value
        img = expected_image(fmt, w, h, self.frame_id)
        if fmt.endswith("12p"):
            data = pack_12p(img.reshape(-1)).tobytes()
        elif fmt.endswith("16"):
            data = img.astype("<u2").tobytes()
        else:
            data = img.tobytes()
        incomplete = self.fault == "incomplete"
        self.fault = None
        buf = FakeBuffer(data, w, h, self.frame_id, incomplete, stamp)
        self.outstanding.append(buf)
        return buf

    def requeue_buffer(self, buf: FakeBuffer) -> None:
        self.outstanding.remove(buf)
        self.requeued += 1


class _StreamNodeMap:
    def __init__(self, device: FakeDevice) -> None:
        self.values: dict[str, Any] = {
            "StreamAutoNegotiatePacketSize": False,
            "StreamPacketResendEnable": False,
            "StreamBufferHandlingMode": "OldestFirstOverwrite",
        }

    def get_node(self, name: str) -> Any:
        if name not in self.values:
            return None
        values = self.values

        class _N:
            @property
            def value(self) -> Any:
                return values[name]

            @value.setter
            def value(self, v: Any) -> None:
                values[name] = v

        return _N()


class FakeSystem:
    def __init__(self, infos: list[dict[str, Any]] | None = None, **dev_kw: Any) -> None:
        self.device_infos = infos if infos is not None else [dict(JAI_INFO), dict(LUCID_INFO)]
        self.created: list[FakeDevice] = []
        self.destroyed: list[FakeDevice] = []
        self.dev_kw = dev_kw

    def create_device(self, device_infos: Any = None) -> list[FakeDevice]:
        if not isinstance(device_infos, dict):
            raise AssertionError("driver must create exactly one device from one info dict")
        dev = FakeDevice(device_infos, **self.dev_kw)
        self.created.append(dev)
        return [dev]

    def destroy_device(self, device: Any = None) -> None:
        if device is None:
            raise AssertionError("driver must destroy only its own device")
        self.destroyed.append(device)
