"""JAI eBUS driver (``ebus_base`` / ``ebus_receive`` 7.0.1) for the JAI Apex AP-3200T-PGE.

Only **JAI** devices are handled: discovery filters ``PvSystem`` results by
vendor and connects to nothing; ``open()`` connects to exactly one device.

Node mapping. Manual = ``Manuals/vision/jai_apex/JAI_Apex_AP-3200T-PGE_manual_v2.2.rag.md``
(line numbers from its ``.toc.md``); "live" = read from the AP-3200T-PGE (s/n U330439,
DeviceVersion 0.1.0.7, DeviceFirmwareVersion 0.0.3.4) on 2026-10-21.

==================  =========================================  ====================================
camera_lab          GenICam node(s)                            Source / live check
==================  =========================================  ====================================
auto functions off  ExposureAuto, GainAuto, BalanceWhiteAuto   AcquisitionControl L3570;
                    = "Off"; ExposureMode = Timed;              AnalogControl L3708; Exposure Mode
                    ExposureTimeMode = Common;                  L1782 (Timed = exposure-time
                    IndividualGainMode = Off                    control; Off = none)
exposure_us         ExposureTime (µs)                           AcquisitionControl L3570. Live:
                    + AcquisitionFrameRate when longer than     1 … 82 402 µs at 12.03 Hz, max
                    the current frame period allows             199 262 µs at 5 Hz: max follows
                                                                the frame period → the driver
                                                                lowers the frame rate (and
                                                                restores free-run afterwards)
gain_db             Gain[GainSelector=AnalogAll], a             Gain Control L2131; Adjust the
                    **multiplier** ×1 … ×8 →                    Gain L1158 ("configured by
                    dB = 20·log10(×)                            multipliers"); Decibel vs
                                                                Multiplier L6044. Live: 1.0 … 8.0
white_balance       red = Gain[AnalogRed], blue =               White Balance L1234; Gain Control
                    Gain[AnalogBlue] (×0.47 … ×4, relative      L2131 (3-CMOS prism: per-channel
                    to master); green = Gain[AnalogGreen]       sensor gain). Live: AnalogGreen
                    fixed ×1 in master mode; DigitalRed/Blue    read-only ×1 when IndividualGain
                    reset to ×1 on open                         Mode = Off
gamma               Gamma (0.45 … 1.0) with LUTMode = Gamma;    Gamma Function L2284; AnalogControl
                    gamma 1.0 ⇒ LUTMode = Off (linear)          L3708. Live: float, node is
                                                                "LUTMode" (manual: "JAI LUT Mode")
pixel_format        PixelFormat: RGB8, RGB10V1Packed,           Pixel Format L1455;
                    RGB10p32, RGB12V1Packed                     ImageFormatControl L3456. Layouts
                                                                verified bit-exactly with the
                                                                camera's test patterns (unpack.py)
roi                 OffsetX, OffsetY, Width, Height             ROI L2494; ImageFormatControl
                    (WidthMax 2064, HeightMax 1544)             L3456. Live: Width/OffsetX inc 16,
                                                                Height/OffsetY inc 2; not writable
                                                                while streaming
frame_rate_hz       AcquisitionFrameRate (0.125 Hz … max)       Acquisition Control L1486;
                                                                AcquisitionControl L3570. No
                                                                enable node: free-run = max
trigger_mode        off: TriggerMode[FrameStart] = Off;         Trigger Control L1834;
                    software: TriggerSelector = FrameStart,     AcquisitionControl L3570. Live:
                    TriggerSource = Software, TriggerMode =     TriggerMode not writable while
                    On, TriggerSoftware.Execute()               streaming
stream              NegotiatePacketSize (or SetPacketSize),     eBUS samples PvPipelineSample /
                    SetStreamDestination, PvPipeline,           GenICamParameters (from the eBUS
                    StreamEnable + AcquisitionStart             Python .deb in SDK/jai_ebus/).
                                                                Live: negotiated GevSCPSPacketSize
                                                                8944 on enp9s0f0 (MTU 9000)
==================  =========================================  ====================================

Writable only while acquisition is stopped (live): PixelFormat, Width, OffsetX
(ROI), TriggerMode, ExposureMode. Exposure, frame rate, gain, gamma and white
balance can change while streaming.

Buffers: ``RetrieveNextBuffer(timeout)`` → copy → ``ReleaseBuffer`` in a
``finally``. Before each grab the pipeline's output queue is drained so the
frame is fresh (like Arena's NewestOnly), and after a settings change while
streaming ``FLUSH_FRAMES_AFTER_APPLY`` more frames are dropped. ``close()``
stops acquisition, stops the pipeline, closes and frees the stream and
disconnects/frees the device, each step in a ``finally``.
"""

from __future__ import annotations

import contextlib
import math
import threading
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, ClassVar

import numpy as np
import numpy.typing as npt

from camera_lab.core.logging import get_logger
from camera_lab.two_d.backups import FeatureAccess, FeatureValue, RestoreReport, multipass_restore
from camera_lab.two_d.drivers.base import IDENTITY_NODES, Camera2D, Camera2DDriver
from camera_lab.two_d.drivers.unpack import unpack_rgb10p32, unpack_rgb10v1, unpack_rgb12v1
from camera_lab.two_d.errors import (
    CameraBusyError,
    CameraClosedError,
    CameraDisconnectedError,
    CameraNotFoundError,
    CaptureTimeoutError,
    DeviceError,
    IncompleteFrameError,
    SdkUnavailableError,
    StreamingStateError,
)
from camera_lab.two_d.models import (
    PIXEL_FORMAT_BITS,
    ROI,
    ApplyResult,
    CameraInfo,
    CameraSettings,
    CameraSettingsPatch,
    FieldResult,
    Frame,
    SettingSpec,
    WbChannel,
)

_log = get_logger(__name__)

JAI_VENDOR = "jai corporation"
SUPPORTED_FORMATS = ("RGB8", "RGB10V1Packed", "RGB10p32", "RGB12V1Packed")
PIPELINE_BUFFERS = 8
FLUSH_FRAMES_AFTER_APPLY = 2
#: Margin between frame period and the longest exposure (live: 83 139 − 82 402 µs).
FRAME_OVERHEAD_US = 1000.0
#: PvSystem default is 1500 ms; both cameras answered well within 500 ms.
DISCOVERY_TIMEOUT_MS = 500
GAMMA_LINEAR = 1.0
_WB_GAIN = {"red": "AnalogRed", "green": "AnalogGreen", "blue": "AnalogBlue"}


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _load_sdk() -> Any:
    try:
        from ebus_base.eBUSBase import PvSystem  # lazy vendor import
        from ebus_receive.eBUSReceive import PvDevice, PvPipeline, PvStream
    except Exception as exc:  # ImportError, or OSError from the native libraries
        raise SdkUnavailableError(f"eBUS SDK not available: {exc}") from exc
    return SimpleNamespace(
        PvSystem=PvSystem, PvDevice=PvDevice, PvStream=PvStream, PvPipeline=PvPipeline
    )


def _db(x: float) -> float:
    return 20.0 * math.log10(x)


def _mult(db: float) -> float:
    return float(10.0 ** (db / 20.0))


def _result_text(result: Any) -> str:
    try:
        desc = result.GetDescription() if result.HasDescription() else ""
    except Exception:
        desc = ""
    return f"{result.GetCodeString()}{': ' + str(desc).strip() if desc else ''}"[:240]


class _ParamError(Exception):
    """A GenICam read/write returned a failed PvResult."""


def _val(x: Any) -> Any:
    """eBUS calls return ``[PvResult, value]``; return the value (or ``x`` itself)."""
    return x[1] if isinstance(x, list | tuple) and len(x) == 2 else x


def _param_names(plist: Any) -> list[str]:
    """Names in a PvGenParameterList (GetFirst / GetNext)."""
    names: list[str] = []
    item = plist.GetFirst() if plist is not None else None
    while item is not None:
        names.append(str(_val(item.GetName())))
        item = plist.GetNext()
    return names


#: Selectors with more entries than this are not expanded (e.g. LUTIndex 0–256).
MAX_SELECTOR_ENTRIES = 16


class _EbusAccess(FeatureAccess):
    """GenICam string access (``ToString`` / ``FromString``) for :func:`multipass_restore`."""

    def __init__(self, params: Any) -> None:
        self._p = params

    def _prm(self, name: str) -> Any:
        prm = self._p.Get(name)
        if prm is None:
            raise _ParamError(f"{name} not present on camera")
        return prm

    def select(self, selector: str, value: str | int) -> None:
        if str(_val(self._prm(selector).ToString())) != str(value):
            self.write(selector, value)

    def read(self, name: str) -> Any:
        return str(_val(self._prm(name).ToString()))

    def writable(self, name: str) -> bool:
        return bool(_val(self._prm(name).IsWritable()))

    def write(self, name: str, value: Any) -> None:
        result = self._prm(name).FromString(str(value))
        if not result.IsOK():
            raise _ParamError(f"write {name}={value!r}: {_result_text(result)}")


class EbusCamera(Camera2D):
    """An open JAI camera."""

    def __init__(
        self,
        sdk: Any,
        device: Any,
        stream: Any,
        info: CameraInfo,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._sdk = sdk
        self._dev = device
        self._stream = stream
        self._p = device.GetParameters()
        self._info = info
        self._clock = clock
        self._lock = threading.RLock()
        self._pipeline: Any = None
        self._streaming = False
        self._closed = False
        self._flush = 0
        self._auto_fps = False
        self._fps_explicit = False
        self._settings: CameraSettings | None = None
        # Discovery's "version" is DeviceVersion (JAI: 0.1.0.7); the firmware is
        # DeviceFirmwareVersion (0.0.3.4) — live AP-3200T-PGE 2026-10-22.
        with contextlib.suppress(Exception):  # best effort: identity only
            if self._param("DeviceFirmwareVersion") is not None:
                fw = str(self._get("DeviceFirmwareVersion")).strip()
                if fw:
                    self._info = info.model_copy(update={"firmware": fw})

    # ----------------------------------------------------------- plumbing
    @property
    def info(self) -> CameraInfo:
        """Camera identity."""
        return self._info

    @property
    def is_streaming(self) -> bool:
        """True while acquisition is running."""
        return self._streaming

    def _check_open(self) -> None:
        if self._closed:
            raise CameraClosedError(f"camera {self._info.serial} is closed")

    def _connected(self) -> bool:
        try:
            return bool(self._dev.IsConnected())
        except Exception:
            return False

    @contextlib.contextmanager
    def _guard(self) -> Iterator[None]:
        self._check_open()
        try:
            yield
        except (
            CameraClosedError,
            StreamingStateError,
            CaptureTimeoutError,
            CameraDisconnectedError,
        ):
            raise
        except Exception as exc:
            if not self._connected():
                raise CameraDisconnectedError(
                    f"camera {self._info.serial} disconnected: {exc}"
                ) from exc
            if isinstance(exc, _ParamError):
                raise DeviceError(str(exc)) from exc
            raise

    def _param(self, name: str) -> Any:
        return self._p.Get(name)

    def _get(self, name: str) -> Any:
        p = self._param(name)
        if p is None:
            raise _ParamError(f"{name} not present on camera")
        if hasattr(p, "GetValueString"):
            result, value = p.GetValueString()
        else:
            result, value = p.GetValue()
        if not result.IsOK():
            raise _ParamError(f"read {name}: {_result_text(result)}")
        return value

    def _set(self, name: str, value: Any) -> None:
        p = self._param(name)
        if p is None:
            raise _ParamError(f"{name} not present on camera")
        result = p.SetValue(value)
        if not result.IsOK():
            raise _ParamError(f"write {name}={value!r}: {_result_text(result)}")

    def _writable(self, name: str) -> bool:
        p = self._param(name)
        if p is None:
            return False
        result, ok = p.IsWritable()
        return bool(result.IsOK() and ok)

    def _range(self, name: str) -> tuple[float, float, float | None] | None:
        p = self._param(name)
        if p is None or not hasattr(p, "GetMin"):
            return None
        r1, lo = p.GetMin()
        r2, hi = p.GetMax()
        if not (r1.IsOK() and r2.IsOK()):
            return None
        inc = None
        if hasattr(p, "GetIncrement"):
            r3, step = p.GetIncrement()
            inc = float(step) if r3.IsOK() and step > 1 else None
        return float(lo), float(hi), inc

    def _entries(self, name: str) -> list[str]:
        p = self._param(name)
        if p is None or not hasattr(p, "GetEntriesCount"):
            return []
        result, count = p.GetEntriesCount()
        out = []
        for i in range(count if result.IsOK() else 0):
            r, entry = p.GetEntryByIndex(i)
            if not r.IsOK():
                continue
            ra, available = entry.IsAvailable()
            rn, entry_name = entry.GetName()
            if ra.IsOK() and available and rn.IsOK():
                out.append(str(entry_name))
        return out

    def _try_set(self, name: str, value: Any) -> bool:
        if self._param(name) is None or not self._writable(name):
            return False
        self._set(name, value)
        return True

    @contextlib.contextmanager
    def _gain_selector(self, entry: str) -> Iterator[None]:
        prev = self._get("GainSelector")
        self._set("GainSelector", entry)
        try:
            yield
        finally:
            self._set("GainSelector", prev)

    # -------------------------------------------------------------- open
    def configure_on_open(self) -> None:
        """Auto functions off, timed exposure, master gain mode, free-run."""
        with self._lock, self._guard():
            for auto in ("ExposureAuto", "GainAuto", "BalanceWhiteAuto"):
                if self._try_set(auto, "Off"):
                    _log.info("ebus_auto_off", camera_id=self._info.serial, node=auto)
            self._try_set("ExposureMode", "Timed")
            self._try_set("ExposureTimeMode", "Common")
            self._try_set("IndividualGainMode", "Off")
            self._try_set("AcquisitionMode", "Continuous")
            self._try_set("TestPattern", "Off")
            for digital in ("DigitalRed", "DigitalBlue"):
                with self._gain_selector(digital):
                    self._try_set("Gain", 1.0)
            for selector in ("FrameStart", "AcquisitionStart"):
                if selector in self._entries("TriggerSelector"):
                    self._set("TriggerSelector", selector)
                    self._try_set("TriggerMode", "Off")
            if self._get("PixelFormat") not in SUPPORTED_FORMATS:
                self._set("PixelFormat", "RGB8")
            self._free_run()
            self._settings = self._read_settings()

    # ------------------------------------------------------ backup / restore
    def _selector_entries(self, prm: Any) -> list[str] | None:
        if hasattr(prm, "GetEntriesCount"):
            out = []
            for i in range(int(_val(prm.GetEntriesCount()))):
                entry = _val(prm.GetEntryByIndex(i))
                if entry is not None and _val(entry.IsAvailable()):
                    out.append(str(_val(entry.GetName())))
            return out if len(out) <= MAX_SELECTOR_ENTRIES else None
        lo, hi = _val(prm.GetMin()), _val(prm.GetMax())
        if isinstance(lo, int) and isinstance(hi, int) and hi - lo < MAX_SELECTOR_ENTRIES:
            return [str(v) for v in range(lo, hi + 1)]
        return None

    def backup_settings(self) -> dict[str, Any]:
        """GenICam walk: every streamable, readable feature; selectors expanded.

        The eBUS Python bindings have no PvConfigurationWriter (checked 7.0.1),
        so this mirrors GenApi feature streaming with ``ToString``.
        """
        with self._lock, self._guard():
            features: list[FeatureValue] = []
            not_expanded: list[str] = []
            for i in range(int(_val(self._p.GetCount()))):
                prm = self._p.Get(i)
                if prm is None or hasattr(prm, "Execute"):
                    continue
                if not (_val(prm.IsStreamable()) and _val(prm.IsReadable())):
                    continue
                if _param_names(_val(prm.GetSelectingParameters())):
                    continue  # recorded under its selector
                name = str(_val(prm.GetName()))
                features.append(
                    FeatureValue(
                        name=name,
                        value=str(_val(prm.ToString())),
                        writable=bool(_val(prm.IsWritable())),
                    )
                )
                if not _val(prm.IsSelector()):
                    continue
                entries = self._selector_entries(prm)
                if entries is None or not _val(prm.IsWritable()):
                    not_expanded.append(name)
                    continue
                prev = str(_val(prm.ToString()))
                try:
                    for entry in entries:
                        if not prm.FromString(entry).IsOK():
                            continue
                        for sub in _param_names(_val(prm.GetSelectedParameters())):
                            sp = self._p.Get(sub)
                            if sp is None or hasattr(sp, "Execute"):
                                continue
                            if not (_val(sp.IsStreamable()) and _val(sp.IsReadable())):
                                continue
                            features.append(
                                FeatureValue(
                                    name=sub,
                                    value=str(_val(sp.ToString())),
                                    writable=bool(_val(sp.IsWritable())),
                                    selector=name,
                                    selector_value=entry,
                                )
                            )
                finally:
                    prm.FromString(prev)
            return {
                "format": "ebus-genicam-v1",
                "features": [f.model_dump(mode="json") for f in features],
                "not_expanded_selectors": not_expanded,
            }

    def restore_settings(
        self, payload: dict[str, Any], protected: Callable[[str], bool] | None = None
    ) -> RestoreReport:
        """Write the walk back (multi-pass, selectors set per feature)."""
        if payload.get("format") != "ebus-genicam-v1":
            raise ValueError(f"not an eBUS backup: {payload.get('format')!r}")
        with self._lock, self._guard():
            if self._streaming:
                raise StreamingStateError("stop the stream before restoring settings")
            features = [FeatureValue.model_validate(f) for f in payload.get("features", [])]
            access = _EbusAccess(self._p)
            skipped = []
            if protected is not None:
                for f in [f for f in features if protected(f.name)]:
                    try:
                        if f.selector is not None and f.selector_value is not None:
                            access.select(f.selector, f.selector_value)
                        now = access.read(f.name)
                    except Exception:
                        now = "<unreadable>"
                    if str(now) != str(f.value):
                        skipped.append(f"{f.key}: backup {f.value!r}, camera {now!r}")
                features = [f for f in features if not protected(f.name)]
            report = multipass_restore(features, access)
            self._auto_fps = False
            self._settings = self._read_settings()
            return report.model_copy(update={"protected_skipped": tuple(skipped)})

    def save_user_set(self, slot: str = "UserSet1", make_default: bool = True) -> dict[str, Any]:
        """UserSetSelector = slot → UserSetSave.

        Live AP-3200T-PGE 2026-10-21: UserSetSelector {Default, UserSet1..3},
        UserSetLoad, UserSetSave; UserSetSave is not writable while acquiring
        ("Node is not writable"), so the stream is stopped around it and restarted.
        A save → change → UserSetLoad round trip restored the saved values. There
        is no UserSetDefault node, so the boot set is not set here (``notes``).
        """
        if not slot.startswith("UserSet"):
            raise ValueError(f"user set must be UserSet1/2/..., not {slot!r}")
        with self._lock, self._guard():
            if slot not in self._entries("UserSetSelector"):
                raise ValueError(f"camera has no user set {slot!r}")
            was_streaming = self._streaming
            if was_streaming:
                self.stop_stream()
            try:
                self._set("UserSetSelector", slot)
                result = self._param("UserSetSave").Execute()
                if not result.IsOK():
                    raise _ParamError(f"UserSetSave: {_result_text(result)}")
            finally:
                if was_streaming:
                    self.start_stream()
            notes = []
            if make_default:
                notes.append(
                    "this camera has no UserSetDefault feature: the boot user set was not "
                    "changed (verify the JAI start-up behaviour in its manual)"
                )
            return {"slot": slot, "default": None, "stored_features": None, "notes": notes}

    def load_user_set(self, slot: str = "Default") -> dict[str, Any]:
        """UserSetSelector = slot → UserSetLoad (stream stopped), then the open-time setup.
        The JAI has no UserSetDefault: it boots from User Set 1 (power-cycle test
        2026-10-21), so a known start needs this load on every open."""
        with self._lock, self._guard():
            if slot not in self._entries("UserSetSelector"):
                raise ValueError(f"camera has no user set {slot!r}")
            was_streaming = self._streaming
            if was_streaming:
                self.stop_stream()
            try:
                self._set("UserSetSelector", slot)
                result = self._param("UserSetLoad").Execute()
                if not result.IsOK():
                    raise _ParamError(f"UserSetLoad: {_result_text(result)}")
                self.configure_on_open()  # with the stream stopped (stream-locked parts)
            finally:
                if was_streaming:
                    self.start_stream()
        return {"slot": slot, "notes": ["open-time setup re-applied (autos off)"]}

    def identity_nodes(self) -> dict[str, str]:
        with self._lock, self._guard():
            out: dict[str, str] = {}
            for name in IDENTITY_NODES:
                if self._param(name) is None:
                    continue
                try:
                    out[name] = str(self._get(name))
                except _ParamError:
                    continue
            return out

    def user_set_info(self) -> dict[str, Any]:
        with self._lock, self._guard():
            return {
                "slots": list(self._entries("UserSetSelector")),
                "boot_set_feature": self._param("UserSetDefault") is not None,
                "boot_set": None,
                "stored_features": None,
                "notes": [
                    "UserSetSave needs the stream stopped (live 2026-10-21)",
                    "boots from User Set 1 after a save (power-cycle test 2026-10-21)",
                ],
            }

    def _free_run(self) -> None:
        """No enable node on the JAI: free-run = AcquisitionFrameRate at its maximum."""
        rng = self._range("AcquisitionFrameRate")
        if rng is not None and self._writable("AcquisitionFrameRate"):
            self._set("AcquisitionFrameRate", rng[1])
        self._auto_fps = False

    # ------------------------------------------------------- capabilities
    def capabilities(self) -> dict[str, SettingSpec]:
        """Live ranges from the node map (they change with format, ROI and frame rate)."""
        with self._lock, self._guard():
            specs: dict[str, SettingSpec] = {}
            exp = self._range("ExposureTime")
            fps = self._range("AcquisitionFrameRate")
            if exp is not None:
                hi = exp[1]
                if fps is not None and fps[0] > 0:
                    hi = max(hi, 1e6 / fps[0] - FRAME_OVERHEAD_US)
                specs["exposure_us"] = SettingSpec(
                    name="exposure_us", kind="float", min=exp[0], max=hi, unit="us"
                )
            with self._gain_selector("AnalogAll"):
                g = self._range("Gain")
            if g is not None:
                specs["gain_db"] = SettingSpec(
                    name="gain_db", kind="float", min=_db(g[0]), max=_db(g[1]), unit="dB"
                )
            gamma = self._range("Gamma")
            if gamma is not None and "Gamma" in self._entries("LUTMode"):
                specs["gamma"] = SettingSpec(name="gamma", kind="float", min=gamma[0], max=gamma[1])
            for ch, entry in _WB_GAIN.items():
                if entry not in self._entries("GainSelector"):
                    continue
                with self._gain_selector(entry):
                    rng = self._range("Gain") if ch != "green" else (1.0, 1.0, None)
                if rng is not None:
                    specs[f"wb_{ch}"] = SettingSpec(
                        name=f"wb_{ch}", kind="float", min=rng[0], max=rng[1]
                    )
            opts = tuple(f for f in SUPPORTED_FORMATS if f in self._entries("PixelFormat"))
            if opts:
                specs["pixel_format"] = SettingSpec(
                    name="pixel_format", kind="enum", options=opts, requires_stream_stop=True
                )
            specs.update(self._roi_specs())
            if fps is not None:
                specs["frame_rate_hz"] = SettingSpec(
                    name="frame_rate_hz", kind="float", min=fps[0], max=fps[1], unit="Hz"
                )
            if "Software" in self._entries("TriggerSource"):
                specs["trigger_mode"] = SettingSpec(
                    name="trigger_mode",
                    kind="enum",
                    options=("off", "software"),
                    requires_stream_stop=True,
                )
            specs.update(self._prism_specs())
            return specs

    def _roi_specs(self) -> dict[str, SettingSpec]:
        w, h = self._range("Width"), self._range("Height")
        ox, oy = self._range("OffsetX"), self._range("OffsetY")
        if None in (w, h, ox, oy):
            return {}
        assert w is not None and h is not None and ox is not None and oy is not None  # noqa: S101
        wmax, hmax = float(self._get("WidthMax")), float(self._get("HeightMax"))
        flags: dict[str, Any] = {
            "affects_calibration": True,
            "requires_stream_stop": True,
            "unit": "px",
        }
        return {
            "roi_x": SettingSpec(
                name="roi_x", kind="int", min=0, max=wmax - w[0], increment=ox[2], **flags
            ),
            "roi_y": SettingSpec(
                name="roi_y", kind="int", min=0, max=hmax - h[0], increment=oy[2], **flags
            ),
            "roi_width": SettingSpec(
                name="roi_width", kind="int", min=w[0], max=wmax, increment=w[2], **flags
            ),
            "roi_height": SettingSpec(
                name="roi_height", kind="int", min=h[0], max=hmax, increment=h[2], **flags
            ),
        }

    # ----------------------------------------------------------- settings
    def _read_wb(self) -> dict[WbChannel, float] | None:
        entries = self._entries("GainSelector")
        if "AnalogRed" not in entries or "AnalogBlue" not in entries:
            return None
        out: dict[WbChannel, float] = {}
        for ch, entry in _WB_GAIN.items():
            with self._gain_selector(entry):
                out[ch] = float(self._get("Gain"))  # type: ignore[index]
        return out

    def _read_settings(self) -> CameraSettings:
        with self._gain_selector("AnalogAll"):
            gain = _db(float(self._get("Gain")))
        lut = self._get("LUTMode") if self._param("LUTMode") is not None else "Off"
        gamma = float(self._get("Gamma")) if lut == "Gamma" else GAMMA_LINEAR
        self._set("TriggerSelector", "FrameStart")
        software = self._get("TriggerMode") == "On" and self._get("TriggerSource") == "Software"
        return CameraSettings(
            exposure_us=float(self._get("ExposureTime")),
            gain_db=gain,
            gamma=gamma,
            white_balance=self._read_wb(),
            pixel_format=str(self._get("PixelFormat")),
            roi=ROI(
                x=int(self._get("OffsetX")),
                y=int(self._get("OffsetY")),
                width=int(self._get("Width")),
                height=int(self._get("Height")),
            ),
            frame_rate_hz=float(self._get("AcquisitionFrameRate")),
            trigger_mode="software" if software else "off",
            advanced=self._read_prism(),
        )

    def get_settings(self) -> CameraSettings:
        """Read every setting back from the camera."""
        with self._lock, self._guard():
            self._settings = self._read_settings()
            return self._settings.model_copy(deep=True)

    # -------------------------------------------------------------- apply
    def _set_number(
        self,
        field: str,
        name: str,
        value: float,
        *,
        to_node: Callable[[float], float] = float,
        from_node: Callable[[float], float] = float,
    ) -> FieldResult:
        if self._param(name) is None:
            return FieldResult(
                field=field,
                status="rejected",
                requested=value,
                reason=f"{name} not present on camera",
            )
        if not self._writable(name):
            return FieldResult(
                field=field,
                status="rejected",
                requested=value,
                reason=f"{name} is not writable now",
            )
        rng = self._range(name)
        raw = to_node(value)
        if rng is not None and not rng[0] - 1e-9 <= raw <= rng[1] + 1e-9:
            return FieldResult(
                field=field,
                status="rejected",
                requested=value,
                reason=f"{field} {value!r} outside camera range "
                f"[{from_node(rng[0]):.6g}, {from_node(rng[1]):.6g}]",
            )
        try:
            self._set(name, raw)
            back = from_node(float(self._get(name)))
        except _ParamError as exc:
            return FieldResult(field=field, status="rejected", requested=value, reason=str(exc))
        if math.isclose(back, value, rel_tol=1e-6, abs_tol=1e-6):
            return FieldResult(field=field, status="applied", requested=value, value=back)
        return FieldResult(
            field=field,
            status="clamped",
            requested=value,
            value=back,
            reason=f"camera set {name} to {back:.6g}",
        )

    def _apply_roi(self, roi: ROI) -> FieldResult:
        req = roi.model_dump()
        wmax, hmax = int(self._get("WidthMax")), int(self._get("HeightMax"))
        if roi.x + roi.width > wmax or roi.y + roi.height > hmax:
            return FieldResult(
                field="roi",
                status="rejected",
                requested=req,
                reason=f"roi exceeds sensor {wmax}x{hmax}",
            )
        prev = {n: int(self._get(n)) for n in ("OffsetX", "OffsetY", "Width", "Height")}
        try:
            self._set("OffsetX", 0)
            self._set("OffsetY", 0)
            self._set("Width", int(roi.width))
            self._set("Height", int(roi.height))
            self._set("OffsetX", int(roi.x))
            self._set("OffsetY", int(roi.y))
        except _ParamError as exc:
            with contextlib.suppress(Exception):
                self._set("OffsetX", 0)
                self._set("OffsetY", 0)
                self._set("Width", prev["Width"])
                self._set("Height", prev["Height"])
                self._set("OffsetX", prev["OffsetX"])
                self._set("OffsetY", prev["OffsetY"])
            return FieldResult(
                field="roi", status="rejected", requested=req, reason=f"ROI not accepted: {exc}"
            )
        back = ROI(
            x=int(self._get("OffsetX")),
            y=int(self._get("OffsetY")),
            width=int(self._get("Width")),
            height=int(self._get("Height")),
        )
        if back == roi:
            return FieldResult(
                field="roi", status="applied", requested=req, value=back.model_dump()
            )
        return FieldResult(
            field="roi",
            status="clamped",
            requested=req,
            value=back.model_dump(),
            reason=f"camera set ROI to {back.model_dump()}",
        )

    def _apply_exposure(
        self,
        value: float,
        *,
        explicit_fps: bool,
        warnings: list[str],
        field: str = "exposure_us",
        allow_restore: bool = True,
    ) -> FieldResult:
        """Set ExposureTime (for the current ExposureTimeSelector).

        Lowers the frame rate when the exposure is longer than the frame period
        allows (unless a frame rate was set explicitly); restores free-run when a
        common exposure fits again (``allow_restore``; per-sensor writes do not
        restore, another sensor may still need the long frame).
        """
        exp = self._range("ExposureTime")
        fps = self._range("AcquisitionFrameRate")
        if (
            exp is not None
            and fps is not None
            and not explicit_fps
            and not self._fps_explicit
            and value > exp[1]
            and self._writable("AcquisitionFrameRate")
        ):
            target = min(fps[1], max(fps[0], 1e6 / (value + FRAME_OVERHEAD_US)))
            self._set("AcquisitionFrameRate", target)
            self._auto_fps = True
            warnings.append(
                f"frame rate lowered to {float(self._get('AcquisitionFrameRate')):.3f} Hz "
                f"to allow exposure {value} us"
            )
            return self._set_number(field, "ExposureTime", value)
        result = self._set_number(field, "ExposureTime", value)
        fits_free_run = fps is not None and value <= 1e6 / fps[1] - FRAME_OVERHEAD_US
        if allow_restore and self._auto_fps and fits_free_run and result.status != "rejected":
            self._free_run()
            warnings.append("frame rate restored to free-run maximum")
        return result

    def _apply_wb(self, wb: dict[WbChannel, float]) -> list[FieldResult]:
        out = []
        entries = self._entries("GainSelector")
        for ch, v in wb.items():
            field = f"white_balance.{ch}"
            entry = _WB_GAIN[ch]
            if entry not in entries:
                out.append(
                    FieldResult(
                        field=field,
                        status="rejected",
                        requested=v,
                        reason="white balance gain not supported",
                    )
                )
                continue
            if ch == "green":
                ok = math.isclose(v, 1.0, abs_tol=1e-9)
                out.append(
                    FieldResult(
                        field=field,
                        status="applied" if ok else "rejected",
                        requested=v,
                        value=1.0 if ok else None,
                        reason=None
                        if ok
                        else "green is the reference (fixed ×1 in master gain mode)",
                    )
                )
                continue
            with self._gain_selector(entry):
                out.append(self._set_number(field, "Gain", v))
        return out

    def _apply_gamma(self, value: float) -> FieldResult:
        if self._param("LUTMode") is None:
            return FieldResult(
                field="gamma",
                status="rejected",
                requested=value,
                reason="gamma not supported (no LUTMode node)",
            )
        try:
            if math.isclose(value, GAMMA_LINEAR, abs_tol=1e-9):
                self._set("LUTMode", "Off")
                return FieldResult(
                    field="gamma", status="applied", requested=value, value=GAMMA_LINEAR
                )
            res = self._set_number("gamma", "Gamma", value)
            if res.status != "rejected":
                self._set("LUTMode", "Gamma")
            return res
        except _ParamError as exc:
            return FieldResult(field="gamma", status="rejected", requested=value, reason=str(exc))

    # ------------------------------------------- prism (3-sensor) advanced
    _PRISM_CH: ClassVar[dict[str, str]] = {"red": "Red", "green": "Green", "blue": "Blue"}

    def _prism_specs(self) -> dict[str, SettingSpec]:
        """Per-sensor exposure and gain modes (live: AP-3200T-PGE, 2026-10-21)."""
        specs: dict[str, SettingSpec] = {}
        if {"Common", "Individual"} <= set(self._entries("ExposureTimeMode")):
            specs["prism.exposure_mode"] = SettingSpec(
                name="prism.exposure_mode",
                kind="enum",
                options=("common", "individual"),
                requires_stream_stop=True,  # live: ExposureTimeMode locked while streaming
            )
            for ch, entry in self._PRISM_CH.items():
                if entry not in self._entries("ExposureTimeSelector"):
                    continue
                with self._exposure_selector(entry):
                    rng = self._range("ExposureTime")
                fps = self._range("AcquisitionFrameRate")
                if rng is not None:
                    hi = rng[1]
                    if fps is not None and fps[0] > 0:  # the frame rate may be lowered
                        hi = max(hi, 1e6 / fps[0] - FRAME_OVERHEAD_US)
                    specs[f"prism.exposure_{ch}_us"] = SettingSpec(
                        name=f"prism.exposure_{ch}_us",
                        kind="float",
                        min=rng[0],
                        max=hi,
                        unit="us",
                    )
        if {"Off", "On"} <= set(self._entries("IndividualGainMode")):
            specs["prism.gain_mode"] = SettingSpec(
                name="prism.gain_mode", kind="enum", options=("master", "individual")
            )
        return specs

    @contextlib.contextmanager
    def _exposure_selector(self, entry: str) -> Iterator[None]:
        prev = self._get("ExposureTimeSelector")
        self._set("ExposureTimeSelector", entry)
        try:
            yield
        finally:
            self._set("ExposureTimeSelector", prev)

    def _read_prism(self) -> dict[str, float | str]:
        out: dict[str, float | str] = {}
        if self._param("ExposureTimeMode") is not None:
            out["prism.exposure_mode"] = str(self._get("ExposureTimeMode")).lower()
            for ch, entry in self._PRISM_CH.items():
                if entry in self._entries("ExposureTimeSelector"):
                    with self._exposure_selector(entry):
                        out[f"prism.exposure_{ch}_us"] = float(self._get("ExposureTime"))
        if self._param("IndividualGainMode") is not None:
            out["prism.gain_mode"] = (
                "individual" if self._get("IndividualGainMode") == "On" else "master"
            )
        return out

    def _apply_prism_mode(self, key: str, value: Any) -> FieldResult:
        field = f"advanced.{key}"
        node, mapping = (
            ("ExposureTimeMode", {"common": "Common", "individual": "Individual"})
            if key == "prism.exposure_mode"
            else ("IndividualGainMode", {"master": "Off", "individual": "On"})
        )
        if value not in mapping or self._param(node) is None:
            return FieldResult(
                field=field,
                status="rejected",
                requested=value,
                reason=f"{key} {value!r} not supported",
            )
        try:
            if self._get(node) != mapping[value]:  # no write if unchanged (locked while streaming)
                self._set(node, mapping[value])
        except _ParamError as exc:
            return FieldResult(field=field, status="rejected", requested=value, reason=str(exc))
        return FieldResult(field=field, status="applied", requested=value, value=value)

    def _apply_prism_exposure(
        self, key: str, value: Any, *, explicit_fps: bool, warnings: list[str]
    ) -> FieldResult:
        field = f"advanced.{key}"
        ch = key.removeprefix("prism.exposure_").removesuffix("_us")
        if ch not in self._PRISM_CH or not isinstance(value, int | float):
            return FieldResult(
                field=field,
                status="rejected",
                requested=value,
                reason=f"advanced setting {key!r} not supported",
            )
        if self._get("ExposureTimeMode") != "Individual":
            return FieldResult(
                field=field,
                status="rejected",
                requested=value,
                reason="per-sensor exposure needs prism.exposure_mode = individual",
            )
        with self._exposure_selector(self._PRISM_CH[ch]):
            return self._apply_exposure(
                float(value),
                explicit_fps=explicit_fps,
                warnings=warnings,
                field=field,
                allow_restore=False,
            )

    def _apply_trigger(self, mode: str) -> FieldResult:
        try:
            self._set("TriggerSelector", "FrameStart")
            if mode == "software":
                self._set("TriggerSource", "Software")
                self._set("TriggerMode", "On")
            else:
                self._set("TriggerMode", "Off")
        except _ParamError as exc:
            return FieldResult(
                field="trigger_mode", status="rejected", requested=mode, reason=f"trigger: {exc}"
            )
        return FieldResult(field="trigger_mode", status="applied", requested=mode, value=mode)

    def apply(self, patch: CameraSettingsPatch) -> ApplyResult:
        """Apply already-validated settings and read every value back."""
        with self._lock, self._guard():
            cur = self._settings or self._read_settings()
            if self._streaming:
                blocked = []
                if patch.pixel_format is not None and patch.pixel_format != cur.pixel_format:
                    blocked.append("pixel_format")
                if patch.roi is not None and patch.roi != cur.roi:
                    blocked.append("roi")
                if patch.trigger_mode is not None and patch.trigger_mode != cur.trigger_mode:
                    blocked.append("trigger_mode")
                new_mode = (patch.advanced or {}).get("prism.exposure_mode")
                if new_mode is not None and new_mode != cur.advanced.get("prism.exposure_mode"):
                    blocked.append("prism.exposure_mode")
                if blocked:
                    raise StreamingStateError(
                        f"stop the stream before changing {', '.join(blocked)}"
                    )
            results: list[FieldResult] = []
            warnings: list[str] = []
            geometry_changed = False
            adv = dict(patch.advanced or {})
            for key in ("prism.gain_mode", "prism.exposure_mode"):  # modes before values
                if key in adv:
                    results.append(self._apply_prism_mode(key, adv.pop(key)))
            if patch.pixel_format is not None:
                if (
                    patch.pixel_format not in SUPPORTED_FORMATS
                    or patch.pixel_format not in self._entries("PixelFormat")
                ):
                    results.append(
                        FieldResult(
                            field="pixel_format",
                            status="rejected",
                            requested=patch.pixel_format,
                            reason=f"pixel_format {patch.pixel_format!r} not supported",
                        )
                    )
                else:
                    try:
                        self._set("PixelFormat", patch.pixel_format)
                        results.append(
                            FieldResult(
                                field="pixel_format",
                                status="applied",
                                requested=patch.pixel_format,
                                value=patch.pixel_format,
                            )
                        )
                        geometry_changed = True
                    except _ParamError as exc:
                        results.append(
                            FieldResult(
                                field="pixel_format",
                                status="rejected",
                                requested=patch.pixel_format,
                                reason=str(exc),
                            )
                        )
            if patch.roi is not None:
                r = self._apply_roi(patch.roi)
                results.append(r)
                geometry_changed = geometry_changed or r.status != "rejected"
            if patch.frame_rate_hz is not None:
                self._fps_explicit = True
                self._auto_fps = False
                results.append(
                    self._set_number("frame_rate_hz", "AcquisitionFrameRate", patch.frame_rate_hz)
                )
            elif geometry_changed and not self._fps_explicit:
                self._free_run()  # the maximum moved with the format/ROI
            if patch.exposure_us is not None:
                results.append(
                    self._apply_exposure(
                        patch.exposure_us,
                        explicit_fps=patch.frame_rate_hz is not None,
                        warnings=warnings,
                    )
                )
            if patch.gain_db is not None:
                with self._gain_selector("AnalogAll"):
                    results.append(
                        self._set_number(
                            "gain_db", "Gain", patch.gain_db, to_node=_mult, from_node=_db
                        )
                    )
            if patch.gamma is not None:
                results.append(self._apply_gamma(patch.gamma))
            if patch.white_balance is not None:
                results.extend(self._apply_wb(patch.white_balance))
            if patch.trigger_mode is not None:
                results.append(self._apply_trigger(patch.trigger_mode))
            for key, value in adv.items():
                results.append(
                    self._apply_prism_exposure(
                        key, value, explicit_fps=patch.frame_rate_hz is not None, warnings=warnings
                    )
                )
            if self._streaming and results:
                self._flush = FLUSH_FRAMES_AFTER_APPLY
            self._settings = self._read_settings()
            return ApplyResult(
                results=tuple(results),
                settings=self._settings.model_copy(deep=True),
                warnings=tuple(warnings),
            )

    # ------------------------------------------------------------- stream
    def start_stream(self) -> None:
        """Pipeline start, stream enable, AcquisitionStart (idempotent)."""
        with self._lock, self._guard():
            if self._streaming:
                return
            pipeline = self._sdk.PvPipeline(self._stream)
            pipeline.SetBufferCount(PIPELINE_BUFFERS)
            pipeline.SetBufferSize(self._dev.GetPayloadSize())
            result = pipeline.Start()
            if result is not None and not result.IsOK():
                raise DeviceError(f"pipeline start: {_result_text(result)}")
            try:
                self._dev.StreamEnable()
                self._param("AcquisitionStart").Execute()
            except BaseException:
                with contextlib.suppress(Exception):
                    self._dev.StreamDisable()
                pipeline.Stop()
                raise
            self._pipeline = pipeline
            self._streaming = True
            self._flush = 0

    def stop_stream(self) -> None:
        """AcquisitionStop, stream disable, pipeline stop (idempotent)."""
        with self._lock:
            if not self._streaming:
                return
            pipeline, self._pipeline = self._pipeline, None
            self._streaming = False
            try:
                self._param("AcquisitionStop").Execute()
            finally:
                try:
                    self._dev.StreamDisable()
                finally:
                    pipeline.Stop()

    def _retrieve(self, timeout_ms: int) -> Any:
        result, buf, op = self._pipeline.RetrieveNextBuffer(int(timeout_ms))
        if not result.IsOK():
            if not self._connected():
                raise CameraDisconnectedError(f"camera {self._info.serial} disconnected")
            if "TIMEOUT" in result.GetCodeString():
                raise CaptureTimeoutError(
                    f"no frame from {self._info.serial} within {timeout_ms} ms"
                )
            raise DeviceError(f"retrieve buffer: {_result_text(result)}")
        return buf, op

    def _drain(self) -> None:
        """Drop frames already queued (and stale frames after an apply)."""
        queued = 0
        with contextlib.suppress(Exception):
            queued = int(self._pipeline.GetOutputQueueSize())
        for _ in range(queued):
            buf, _op = self._retrieve(0)
            self._pipeline.ReleaseBuffer(buf)

    def grab(self, timeout_ms: int = 2000) -> Frame:
        """Return a fresh frame as a copy; the buffer is always released."""
        with self._lock, self._guard():
            if timeout_ms <= 0:
                raise ValueError("timeout_ms must be > 0")
            if not self._streaming:
                raise StreamingStateError("camera is not streaming; call start_stream() first")
            settings = self._settings or self._read_settings()
            self._drain()
            if settings.trigger_mode == "software":
                self._flush = 0
                self._param("TriggerSoftware").Execute()
            else:
                while self._flush > 0:
                    buf, _op = self._retrieve(timeout_ms)
                    self._pipeline.ReleaseBuffer(buf)
                    self._flush -= 1
            buf, op = self._retrieve(timeout_ms)
            try:
                if op is not None and not op.IsOK():
                    raise IncompleteFrameError(
                        f"incomplete frame from {self._info.serial}: {_result_text(op)}"
                    )
                img = buf.GetImage()
                w, h = int(img.GetWidth()), int(img.GetHeight())
                raw = np.array(buf.GetDataPointer(), dtype=np.uint8, copy=True).reshape(-1)
                frame_id = int(buf.GetBlockID())
            finally:
                self._pipeline.ReleaseBuffer(buf)
            return Frame(
                data=decode(raw, settings.pixel_format, w, h),
                pixel_format=settings.pixel_format,
                bit_depth=PIXEL_FORMAT_BITS[settings.pixel_format],
                timestamp_utc=self._clock(),
                frame_id=frame_id,
                settings=settings.model_copy(deep=True),
            )

    def close(self) -> None:
        """Stop acquisition and release stream + device (each step in finally)."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                try:
                    self.stop_stream()
                except Exception as exc:
                    _log.warning("ebus_stop_failed", error=str(exc))
            finally:
                try:
                    self._stream.Close()
                    self._sdk.PvStream.Free(self._stream)
                finally:
                    self._dev.Disconnect()
                    self._sdk.PvDevice.Free(self._dev)
                    _log.info("ebus_device_disconnected", camera_id=self._info.serial)


def decode(
    raw: npt.NDArray[np.uint8], pixel_format: str, width: int, height: int
) -> npt.NDArray[Any]:
    """Raw buffer bytes → HxWx3 array (uint8 for RGB8, uint16 native values otherwise)."""
    n = width * height
    if pixel_format == "RGB8":
        if raw.size < n * 3:
            raise IncompleteFrameError(f"payload {raw.size} bytes < expected {n * 3}")
        return raw[: n * 3].reshape(height, width, 3)
    try:
        if pixel_format == "RGB10p32":
            out = unpack_rgb10p32(raw, n)
        elif pixel_format == "RGB10V1Packed":
            out = unpack_rgb10v1(raw, n)
        elif pixel_format == "RGB12V1Packed":
            out = unpack_rgb12v1(raw, n)
        else:
            raise ValueError(f"unsupported pixel format {pixel_format!r}")
    except ValueError as exc:
        if "too short" in str(exc):
            raise IncompleteFrameError(str(exc)) from exc
        raise
    return out.reshape(height, width, 3)


class EbusDriver(Camera2DDriver):
    """Discovers and opens JAI cameras through eBUS (skips every other vendor)."""

    name: ClassVar[str] = "ebus"

    def __init__(
        self,
        sdk: Any = None,
        *,
        clock: Callable[[], datetime] = _utc_now,
        discovery_timeout_ms: int = DISCOVERY_TIMEOUT_MS,
        packet_size: int | None = None,
    ) -> None:
        self._sdk = sdk if sdk is not None else _load_sdk()
        self._clock = clock
        self._timeout_ms = int(discovery_timeout_ms)
        self._packet_size = packet_size
        self.sdk_version = "7.0.1" if sdk is None else "fake"

    def _jai_infos(self) -> list[Any]:
        system = self._sdk.PvSystem()
        with contextlib.suppress(Exception):
            system.SetDetectionTimeout(self._timeout_ms)
        result = system.Find()
        if result is not None and not result.IsOK():
            raise DeviceError(f"eBUS device discovery failed: {_result_text(result)}")
        infos = [system.GetDeviceInfo(i) for i in range(system.GetDeviceCount())]
        self._system = system  # keep alive while the infos are used
        return [i for i in infos if str(i.GetVendorName()).strip().casefold() == JAI_VENDOR]

    @staticmethod
    def _to_info(i: Any) -> CameraInfo:
        ip = i.GetIPAddress() if hasattr(i, "GetIPAddress") else None
        return CameraInfo(
            vendor=str(i.GetVendorName()),
            model=str(i.GetModelName()),
            serial=str(i.GetSerialNumber()),
            firmware=str(i.GetVersion()) or None,
            interface="GigE",
            ip=str(ip) if ip else None,
            driver="ebus",
        )

    def discover(self) -> list[CameraInfo]:
        """List JAI cameras (no connection is made)."""
        return [self._to_info(i) for i in self._jai_infos()]

    def open(self, camera_id: str) -> EbusCamera:
        """Connect to exactly this JAI camera, open its stream and configure it."""
        match = [i for i in self._jai_infos() if str(i.GetSerialNumber()) == camera_id]
        if not match:
            raise CameraNotFoundError(f"no JAI camera with serial {camera_id!r}")
        info = match[0]
        result, device = self._sdk.PvDevice.CreateAndConnect(info)
        if device is None or not result.IsOK():
            raise CameraBusyError(
                f"camera {camera_id} could not be connected ({_result_text(result)}); "
                "is it controlled by another application (e.g. eBUS Player)?",
                holder="another GigE Vision controller",
            )
        stream = None
        try:
            result, stream = self._sdk.PvStream.CreateAndOpen(info.GetConnectionID())
            if stream is None or not result.IsOK():
                raise DeviceError(f"stream open failed: {_result_text(result)}")
            if self._packet_size:
                device.SetPacketSize(int(self._packet_size))
            else:
                device.NegotiatePacketSize()
            device.SetStreamDestination(stream.GetLocalIPAddress(), stream.GetLocalPort())
            cam = EbusCamera(self._sdk, device, stream, self._to_info(info), self._clock)
            # back up before camera_lab changes anything (Derek, 2026-10-21); the walk
            # is cheap, so every open takes one (identical backups are stored once)
            cam.open_backup = cam.backup_settings()
            cam.configure_on_open()
        except BaseException:
            try:
                if stream is not None:
                    stream.Close()
                    self._sdk.PvStream.Free(stream)
            finally:
                device.Disconnect()
                self._sdk.PvDevice.Free(device)
            raise
        _log.info("ebus_device_opened", camera_id=camera_id)
        return cam
