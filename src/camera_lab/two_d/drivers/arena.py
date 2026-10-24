"""Lucid Arena driver (``arena_api`` 2.10.0) for Triton cameras, e.g. TRI054S-C.

Only **Lucid** devices are handled: discovery filters ``system.device_infos`` by
vendor and creates **no** device; ``open()`` creates exactly one device from its
own device-info dict (``system.create_device(info)``), never ``create_device()``
without arguments, which would also open the JAI camera through Arena.

Node mapping. Manual = ``Manuals/vision/lucid_triton/Lucid_Triton_TRI054S_fw1.138.0.0.rag.md``
(line numbers from its ``.toc.md``); "live" = read from the TRI054S-C (s/n 233100505,
fw 1.138.0.0) node map on 2026-10-21.

==================  ==========================================  ===================================
camera_lab          GenICam node(s)                             Source / live check
==================  ==========================================  ===================================
auto functions off  ExposureAuto, GainAuto, BalanceWhiteAuto    Acquisition Control L1643; Analog
                    = "Off" on open                             Control L1783. Live: auto white
                                                                balance was "Continuous" → Off
exposure_us         ExposureTime (µs, float, no increment)      Acquisition Control L1643.
                    + AcquisitionFrameRate when longer than     Live: max = frame period − ~170 µs
                    the current frame period allows             (47 951 µs at 20.787 Hz, 199 833 µs
                                                                at 5 Hz); the camera does NOT
                                                                lengthen the frame itself → the
                                                                driver lowers the frame rate (and
                                                                restores free-run afterwards).
                                                                Read-only while HDRTuningEnable
                                                                is True (HDR mode)
gain_db             Gain (dB), GainSelector = All               Gain L1044; Analog Control L1783.
                                                                Live: 0 … 42 dB (TRI054S is not in
                                                                the manual's gain table)
gamma               Gamma (+ GammaEnable)                       Gamma L1158 (X = Y^Gamma, 0.2–2).
                                                                Live: **no Gamma/GammaEnable node**
                                                                → reported as unsupported
white_balance       BalanceRatioSelector {Red,Green,Blue} +     White Balance L1120; Analog Control
                    BalanceRatio; BalanceWhiteEnable = True     L1783. Live: ratio 0 … 256
pixel_format        PixelFormat                                 Color Processing L1111; Pixel Packer
                                                                L1184; Image Format Control L2198.
                                                                Live: Mono8/12p/16, BayerRG8/12p/16,
                                                                RGB8/12p/16 used (BGR*, YCbCr*, *24
                                                                not offered by camera_lab)
roi                 OffsetX, OffsetY, Width, Height (+WidthMax, ROI L1323; Image Format Control
                    HeightMax)                                  L2198. Live: Width inc 4, Height inc
                                                                2, OffsetX inc 4, OffsetY inc 2,
                                                                sensor 2880 × 1860. The manual
                                                                requires AcquisitionStop before any
                                                                ROI change → all ROI parts stop the
                                                                stream
frame_rate_hz       AcquisitionFrameRateEnable = True +         Acquisition Control L1643. Live: max
                    AcquisitionFrameRate (Hz)                   ≈ 6.56 Hz at RGB8 full frame (link
                                                                limited, 1 GbE)
trigger_mode        off: TriggerMode = Off; software:           Triggering L1206, Trigger Source
                    TriggerSelector = FrameStart,               L1257; Acquisition Control L1643;
                    TriggerSource = Software, TriggerMode = On, example py_trigger.py
                    wait TriggerArmed, TriggerSoftware.execute
stream              StreamAutoNegotiatePacketSize = True,       examples py_simple_acquisition.py /
                    StreamPacketResendEnable = True,            py_exposure.py (TL stream node map);
                    StreamBufferHandlingMode = NewestOnly       Bandwidth Management L499
==================  ==========================================  ===================================

Buffers: ``get_buffer(timeout)`` → copy the bytes (``pdata``/``payload_size``) →
``requeue_buffer`` in a ``finally``. PFNC ``*12p``/``*10p`` data is unpacked to
uint16 native values. ``close()`` always calls ``system.destroy_device(device)``.
A timeout while ``device.is_connected()`` is False becomes
:class:`CameraDisconnectedError`.

After a settings change while streaming, grabbed frames must reflect the new
settings. The driver latches the device clock (TimestampLatch →
TimestampLatchValue, ns) after the writes and discards frames whose
``buffer.timestamp_ns`` is earlier than that plus ``SETTLE_PERIODS`` frame
periods (the longer of the old and new period). Live 2026-10-21: TRI089S-M, a
frame stamped 54 ms after an exposure write (period 79 ms) still had the old
exposure; TRI054S-C (rolling shutter), frames at +12 and +60 ms (period 48 ms)
were stale and the first new one came at +108 ms. A fixed 2-frame flush let
stale frames through. Without
the timestamp nodes the driver falls back to discarding
``FLUSH_FRAMES_AFTER_APPLY`` frames.
"""

from __future__ import annotations

import contextlib
import ctypes
import difflib
import math
import re
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

import numpy as np
import numpy.typing as npt

from camera_lab.core.logging import get_logger
from camera_lab.two_d.backups import FeatureAccess, FeatureValue, RestoreReport, multipass_restore
from camera_lab.two_d.drivers.base import IDENTITY_NODES, Camera2D, Camera2DDriver
from camera_lab.two_d.drivers.unpack import unpack_10p, unpack_12p, unpack_24
from camera_lab.two_d.errors import (
    CameraClosedError,
    CameraDisconnectedError,
    CameraNotFoundError,
    CaptureTimeoutError,
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

LUCID_VENDOR = "lucid vision labs"
#: Pixel formats camera_lab can decode, in the order offered.
SUPPORTED_FORMATS = (
    "Mono8",
    "Mono12p",
    "Mono16",
    "BayerRG8",
    "BayerRG12p",
    "BayerRG16",
    "RGB8",
    "RGB12p",
    "RGB16",
    # 24-bit linear HDR (TRI054S, Lucid's raw path: LUT off): unpacked to uint32
    "Mono24",
    "BayerRG24",
)
STREAM_BUFFERS = 8
#: Discovery wait. arena_api 2.10.0 defaults to 1000 ms (the 2.9.5 docs say 100 ms)
#: and enumeration took ~3 × the timeout on this host; Lucid devices answer
#: within 100 ms, so 300 ms keeps a margin (measured ≈ 0.9 s for both cameras).
DISCOVERY_TIMEOUT_MS = 300
FLUSH_FRAMES_AFTER_APPLY = 2
#: Frames stamped less than this many frame periods after a write may still use the
#: old settings (live 2026-10-21: TRI089S-M stale up to +54 ms of a 79 ms period;
#: TRI054S-C stale up to +60 ms of a 48 ms period, first new frame at +108 ms).
SETTLE_PERIODS = 2
MAX_STALE_FRAMES = 20  # safety stop for the timestamp settle loop
#: ExposureTime.max ≈ frame period − readout overhead (live: 199 833 µs at 5 Hz,
#: 47 951 µs at 20.787 Hz). This margin is used when choosing a frame rate.
FRAME_OVERHEAD_US = 500.0
TRIGGER_ARM_POLL_S = 0.002
_WB_ENTRIES: dict[WbChannel, str] = {"red": "Red", "green": "Green", "blue": "Blue"}


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _load_system() -> Any:
    try:
        from arena_api.system import system  # lazy vendor import
    except Exception as exc:  # ImportError, or OSError from the native libraries
        raise SdkUnavailableError(f"arena_api not available: {exc}") from exc
    return system


def _sdk_version() -> str | None:
    try:
        from arena_api import version

        return str(getattr(version, "__version__", None) or "") or None
    except Exception:  # pragma: no cover - best effort
        return None


def _channels(fmt: str) -> int:
    return 3 if fmt.startswith("RGB") else 1


class ArenaCamera(Camera2D):
    """An open Lucid camera."""

    def __init__(
        self,
        system: Any,
        device: Any,
        info: CameraInfo,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._system = system
        self._dev = device
        self._nm = device.nodemap
        self._info = info
        self._clock = clock
        self._lock = threading.RLock()
        self._streaming = False
        self._closed = False
        self._flush = 0
        self._settle_ns: int | None = None  # frames stamped earlier are stale
        self._settings: CameraSettings | None = None
        self._auto_fps = False  # frame rate lowered by the driver for a long exposure

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
            return bool(self._dev.is_connected())
        except Exception:
            return False

    @contextlib.contextmanager
    def _guard(self) -> Iterator[None]:
        """Map SDK failures on a lost device to CameraDisconnectedError."""
        self._check_open()
        try:
            yield
        except (CameraClosedError, StreamingStateError, CaptureTimeoutError):
            raise
        except Exception as exc:
            if not self._connected():
                raise CameraDisconnectedError(
                    f"camera {self._info.serial} disconnected: {exc}"
                ) from exc
            raise

    def _node(self, name: str) -> Any:
        try:
            return self._nm.get_node(name)
        except Exception:
            return None

    def _writable_node(self, name: str) -> Any:
        """The node if it is readable and writable (a real, settable feature)."""
        node = self._readable_node(name)
        return node if node is not None and self._attr(node, "is_writable") else None

    def _readable_node(self, name: str) -> Any:
        """The node, or None if it is missing or not readable.

        The TRI089S-M (mono, fw 1.97.0.0) lists BalanceRatioSelector /
        BalanceRatio / BalanceWhiteAuto but they are neither readable nor
        writable (verified live 2026-10-21).
        """
        node = self._node(name)
        return node if node is not None and self._attr(node, "is_readable") else None

    @staticmethod
    def _attr(node: Any, name: str) -> Any:
        try:
            return getattr(node, name)
        except Exception:
            return None

    def _set_if_present(self, name: str, value: Any) -> bool:
        node = self._node(name)
        if node is None or not self._attr(node, "is_writable"):
            return False
        node.value = value
        return True

    # -------------------------------------------------------------- open
    def open_changes_needed(self) -> list[str]:
        """What :meth:`configure_on_open` would change on the camera (empty = nothing)."""
        changes = []
        for auto in ("ExposureAuto", "GainAuto", "BalanceWhiteAuto"):
            node = self._node(auto)
            if node is not None and self._attr(node, "is_writable") and node.value != "Off":
                changes.append(f"{auto} {node.value} -> Off")
        for name, want in (
            ("BalanceWhiteEnable", True),
            ("AcquisitionMode", "Continuous"),
            ("TriggerMode", "Off"),
        ):
            node = self._node(name)
            if node is not None and self._attr(node, "is_writable") and node.value != want:
                changes.append(f"{name} {node.value} -> {want}")
        fmt = self._node("PixelFormat")
        if fmt is not None and fmt.value not in SUPPORTED_FORMATS:
            changes.append(f"PixelFormat {fmt.value} -> BayerRG8")
        fps = self._fps_nodes()
        longest = self._longest_exposure()
        if (
            fps is not None
            and longest is not None
            and fps[0].value
            and longest <= 1e6 / float(fps[1].max) - FRAME_OVERHEAD_US
        ):
            changes.append("AcquisitionFrameRateEnable True -> False (free-run)")
        return changes

    def configure_on_open(self) -> None:
        """Turn off every auto function, trigger off, free-run frame rate, stream nodes."""
        with self._lock, self._guard():
            for auto in ("ExposureAuto", "GainAuto", "BalanceWhiteAuto"):
                if self._set_if_present(auto, "Off"):
                    _log.info("arena_auto_off", camera_id=self._info.serial, node=auto)
            self._set_if_present("BalanceWhiteEnable", True)
            self._set_if_present("AcquisitionMode", "Continuous")
            self._set_if_present("TriggerMode", "Off")
            tl = self._dev.tl_stream_nodemap
            for name, value in (
                ("StreamAutoNegotiatePacketSize", True),
                ("StreamPacketResendEnable", True),
                ("StreamBufferHandlingMode", "NewestOnly"),
            ):
                node = tl.get_node(name)
                # not writable while streaming (GUI live view, 2026-10-24): they were set at
                # open and the stream keeps them, so skipping here changes nothing
                if node is not None and self._attr(node, "is_writable") is not False:
                    node.value = value
            fmt = self._node("PixelFormat")
            if fmt is not None and fmt.value not in SUPPORTED_FORMATS:
                fmt.value = "BayerRG8"
            self._restore_free_run(self._longest_exposure(), None)  # free-run by default
            self._settings = self._read_settings()

    # ------------------------------------------------------- capabilities
    def _num_spec(
        self, name: str, node_name: str, *, kind: str = "float", unit: str = "", **flags: Any
    ) -> SettingSpec | None:
        node = self._node(node_name)
        if node is None or not self._attr(node, "is_readable"):
            return None
        lo, hi = self._attr(node, "min"), self._attr(node, "max")
        if lo is None or hi is None:
            return None
        inc = self._attr(node, "inc")
        return SettingSpec(
            name=name,
            kind=kind,  # type: ignore[arg-type]
            min=float(lo),
            max=float(hi),
            increment=float(inc) if isinstance(inc, int | float) and inc > 0 else None,
            unit=unit,
            **flags,
        )

    def _read_only(self, node_name: str) -> bool:
        node = self._node(node_name)
        return node is not None and not self._attr(node, "is_writable")

    def capabilities(self) -> dict[str, SettingSpec]:
        """Live ranges from the node map (they change with format, ROI and frame rate).

        With HDR on, ExposureTime and Gain are read-only and left out (the HDR
        channel settings replace them), so the service reports them as not
        adjustable instead of warning about their read-back values.
        """
        with self._lock, self._guard():
            hdr_locked = self._hdr_on()  # ExposureTime / Gain are read-only in HDR mode
            specs: list[SettingSpec | None] = [
                None if hdr_locked and self._read_only("ExposureTime") else self._exposure_spec(),
                None
                if hdr_locked and self._read_only("Gain")
                else self._num_spec("gain_db", "Gain", unit="dB"),
                self._num_spec("gamma", "Gamma"),
                self._num_spec("frame_rate_hz", "AcquisitionFrameRate", unit="Hz"),
            ]
            if self._readable_node("BalanceRatioSelector") is not None:
                for ch in ("red", "green", "blue"):
                    specs.append(self._num_spec(f"wb_{ch}", "BalanceRatio"))
            fmt = self._node("PixelFormat")
            if fmt is not None:
                entries = list(self._attr(fmt, "enumentry_names") or [])
                opts = tuple(f for f in SUPPORTED_FORMATS if f in entries)
                if opts:
                    specs.append(
                        SettingSpec(
                            name="pixel_format",
                            kind="enum",
                            options=opts,
                            requires_stream_stop=True,
                        )
                    )
            specs.extend(self._roi_specs())
            src = self._node("TriggerSource")
            if src is not None and "Software" in (self._attr(src, "enumentry_names") or []):
                specs.append(
                    SettingSpec(name="trigger_mode", kind="enum", options=("off", "software"))
                )
            out = {s.name: s for s in specs if s is not None}
            out.update(self._hdr_specs())
            return out

    def _roi_specs(self) -> list[SettingSpec]:
        w, h = self._node("Width"), self._node("Height")
        ox, oy = self._node("OffsetX"), self._node("OffsetY")
        wmax, hmax = self._node("WidthMax"), self._node("HeightMax")
        if None in (w, h, ox, oy, wmax, hmax):
            return []
        flags: dict[str, Any] = {
            "affects_calibration": True,
            "requires_stream_stop": True,
            "unit": "px",
        }
        return [
            SettingSpec(
                name="roi_x", kind="int", min=0, max=wmax.value - w.min, increment=ox.inc, **flags
            ),
            SettingSpec(
                name="roi_y", kind="int", min=0, max=hmax.value - h.min, increment=oy.inc, **flags
            ),
            SettingSpec(
                name="roi_width", kind="int", min=w.min, max=wmax.value, increment=w.inc, **flags
            ),
            SettingSpec(
                name="roi_height", kind="int", min=h.min, max=hmax.value, increment=h.inc, **flags
            ),
        ]

    # ----------------------------------------------------------- settings
    def _read_wb(self) -> dict[WbChannel, float] | None:
        sel = self._readable_node("BalanceRatioSelector")
        ratio = self._readable_node("BalanceRatio")
        if sel is None or ratio is None:
            return None
        prev = sel.value
        out: dict[WbChannel, float] = {}
        try:
            for ch, entry in _WB_ENTRIES.items():
                sel.value = entry
                out[ch] = float(ratio.value)
        finally:
            sel.value = prev
        return out

    def _read_settings(self) -> CameraSettings:
        nm = self._nm
        gamma = self._node("Gamma")
        rate = self._node("AcquisitionFrameRate")
        trig_on = self._node("TriggerMode")
        src = self._node("TriggerSource")
        software = (
            trig_on is not None
            and trig_on.value == "On"
            and src is not None
            and src.value == "Software"
        )
        return CameraSettings(
            exposure_us=float(nm.get_node("ExposureTime").value),
            gain_db=float(nm.get_node("Gain").value),
            gamma=float(gamma.value) if gamma is not None else None,
            white_balance=self._read_wb(),
            pixel_format=str(nm.get_node("PixelFormat").value),
            roi=ROI(
                x=int(nm.get_node("OffsetX").value),
                y=int(nm.get_node("OffsetY").value),
                width=int(nm.get_node("Width").value),
                height=int(nm.get_node("Height").value),
            ),
            frame_rate_hz=float(rate.value) if rate is not None else None,
            trigger_mode="software" if software else "off",
            advanced=self._read_hdr(),
        )

    def get_settings(self) -> CameraSettings:
        """Read every setting back from the camera."""
        with self._lock, self._guard():
            self._settings = self._read_settings()
            return self._settings.model_copy(deep=True)

    # -------------------------------------------------------------- apply
    def _set_number(self, field: str, node_name: str, value: float) -> FieldResult:
        node = self._node(node_name)
        if node is None:
            return FieldResult(
                field=field,
                status="rejected",
                requested=value,
                reason=f"{node_name} not present on camera",
            )
        if not self._attr(node, "is_writable"):
            return FieldResult(
                field=field,
                status="rejected",
                requested=value,
                reason=f"{node_name} is not writable now",
            )
        lo, hi = self._attr(node, "min"), self._attr(node, "max")
        if (lo is not None and value < lo) or (hi is not None and value > hi):
            return FieldResult(
                field=field,
                status="rejected",
                requested=value,
                reason=f"{field} {value!r} outside camera range [{lo}, {hi}]",
            )
        try:
            current = node.value
            is_int = isinstance(current, int) and not isinstance(current, bool)
            node.value = round(value) if is_int else float(value)
        except Exception as exc:
            return FieldResult(
                field=field,
                status="rejected",
                requested=value,
                reason=f"{node_name}: {str(exc).strip()[:200]}",
            )
        back = float(node.value)
        inc = self._attr(node, "inc")
        tol = max(1e-9 * max(1.0, abs(value)), (inc / 2) if isinstance(inc, int | float) else 0.0)
        if math.isclose(back, value, rel_tol=1e-6, abs_tol=tol):
            return FieldResult(field=field, status="applied", requested=value, value=back)
        return FieldResult(
            field=field,
            status="clamped",
            requested=value,
            value=back,
            reason=f"camera set {node_name} to {back}",
        )

    def _apply_roi(self, roi: ROI) -> FieldResult:
        req = roi.model_dump()
        wmax, hmax = int(self._node("WidthMax").value), int(self._node("HeightMax").value)
        if roi.x + roi.width > wmax or roi.y + roi.height > hmax:
            return FieldResult(
                field="roi",
                status="rejected",
                requested=req,
                reason=f"roi exceeds sensor {wmax}x{hmax}",
            )
        nodes = {n: self._node(n) for n in ("OffsetX", "OffsetY", "Width", "Height")}
        prev = {n: int(node.value) for n, node in nodes.items()}
        if (
            ROI(x=prev["OffsetX"], y=prev["OffsetY"], width=prev["Width"], height=prev["Height"])
            == roi
        ):
            # unchanged: no write (these nodes are locked while streaming)
            return FieldResult(field="roi", status="applied", requested=req, value=req)
        try:
            nodes["OffsetX"].value = 0
            nodes["OffsetY"].value = 0
            nodes["Width"].value = int(roi.width)
            nodes["Height"].value = int(roi.height)
            nodes["OffsetX"].value = int(roi.x)
            nodes["OffsetY"].value = int(roi.y)
        except Exception as exc:
            with contextlib.suppress(Exception):
                nodes["OffsetX"].value = 0
                nodes["OffsetY"].value = 0
                nodes["Width"].value = prev["Width"]
                nodes["Height"].value = prev["Height"]
                nodes["OffsetX"].value = prev["OffsetX"]
                nodes["OffsetY"].value = prev["OffsetY"]
            return FieldResult(
                field="roi",
                status="rejected",
                requested=req,
                reason=f"ROI not accepted: {str(exc).strip()[:200]}",
            )
        back = ROI(
            x=int(nodes["OffsetX"].value),
            y=int(nodes["OffsetY"].value),
            width=int(nodes["Width"].value),
            height=int(nodes["Height"].value),
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
                if blocked:
                    raise StreamingStateError(
                        f"stop the stream before changing {', '.join(blocked)}"
                    )
            results: list[FieldResult] = []
            if patch.pixel_format is not None:
                fmt = self._node("PixelFormat")
                entries = list(self._attr(fmt, "enumentry_names") or []) if fmt is not None else []
                usable = patch.pixel_format in SUPPORTED_FORMATS and patch.pixel_format in entries
                if fmt is None or not usable:
                    results.append(
                        FieldResult(
                            field="pixel_format",
                            status="rejected",
                            requested=patch.pixel_format,
                            reason=f"pixel_format {patch.pixel_format!r} not supported",
                        )
                    )
                else:
                    if (
                        fmt.value != patch.pixel_format
                    ):  # locked while streaming: write only on change
                        fmt.value = patch.pixel_format
                    results.append(
                        FieldResult(
                            field="pixel_format",
                            status="applied",
                            requested=patch.pixel_format,
                            value=str(fmt.value),
                        )
                    )
            if patch.roi is not None:
                results.append(self._apply_roi(patch.roi))
            warnings: list[str] = []
            if patch.frame_rate_hz is not None:
                self._auto_fps = False  # an explicit frame rate always wins
                fps = self._fps_nodes()
                if fps is None:
                    results.append(
                        self._set_number(
                            "frame_rate_hz", "AcquisitionFrameRate", patch.frame_rate_hz
                        )
                    )
                else:
                    # locked while streaming (live TRI054S-C): pause the stream for the write
                    with self._fps_writable(fps[0]):
                        fps[0].value = True
                        results.append(
                            self._set_number(
                                "frame_rate_hz", "AcquisitionFrameRate", patch.frame_rate_hz
                            )
                        )
            if patch.exposure_us is not None:
                results.append(
                    self._apply_exposure(
                        patch.exposure_us,
                        explicit_fps=patch.frame_rate_hz is not None,
                        warnings=warnings,
                    )
                )
            if patch.gain_db is not None:
                results.append(self._apply_gain(patch.gain_db))
            if patch.gamma is not None:
                self._set_if_present("GammaEnable", True)
                results.append(self._set_number("gamma", "Gamma", patch.gamma))
            if patch.white_balance is not None:
                results.extend(self._apply_wb(patch.white_balance))
            if patch.advanced:
                results.extend(
                    self._apply_hdr(
                        patch.advanced,
                        explicit_fps=patch.frame_rate_hz is not None,
                        warnings=warnings,
                    )
                )
            if patch.trigger_mode is not None:
                results.append(self._apply_trigger(patch.trigger_mode))
            self._settings = self._read_settings()
            if self._streaming and results:
                self._mark_settle(cur.frame_rate_hz, self._settings.frame_rate_hz)
            return ApplyResult(
                results=tuple(results),
                settings=self._settings.model_copy(deep=True),
                warnings=tuple(warnings),
            )

    def _mark_settle(self, *rates_hz: float | None) -> None:
        """Mark frames stamped before 'now + one frame period' (device clock) as stale."""
        latch, latched = self._node("TimestampLatch"), self._node("TimestampLatchValue")
        rates = [r for r in rates_hz if r]
        if latch is None or latched is None or not rates:
            self._flush, self._settle_ns = FLUSH_FRAMES_AFTER_APPLY, None
            return
        try:
            latch.execute()
            now_ns = int(latched.value)
        except Exception as exc:
            _log.warning("arena_timestamp_latch_failed", error=str(exc).strip()[:200])
            self._flush, self._settle_ns = FLUSH_FRAMES_AFTER_APPLY, None
            return
        self._flush = 0
        self._settle_ns = now_ns + SETTLE_PERIODS * int(1e9 / min(rates))

    # ------------------------------------------------- exposure ↔ frame rate
    def _hdr_on(self) -> bool:
        node = self._node("HDRTuningEnable")
        try:
            return bool(node is not None and node.value)
        except Exception:
            return False

    def _fps_nodes(self) -> tuple[Any, Any] | None:
        """Frame-rate nodes, if the driver can change them now or after a stream stop.

        The Triton locks AcquisitionFrameRateEnable / AcquisitionFrameRate
        while streaming (verified live 2026-10-21); :meth:`_fps_writable`
        pauses the stream for the write.
        """
        en, rate = self._node("AcquisitionFrameRateEnable"), self._node("AcquisitionFrameRate")
        if en is None or rate is None:
            return None
        if not self._attr(en, "is_writable") and not self._streaming:
            return None
        return en, rate

    @contextlib.contextmanager
    def _fps_writable(self, en: Any) -> Iterator[None]:
        """Pause the stream around a frame-rate write when the nodes are locked."""
        if self._attr(en, "is_writable") or not self._streaming:
            yield
            return
        self._dev.stop_stream()
        try:
            yield
        finally:
            try:
                self._dev.start_stream(STREAM_BUFFERS)
            except Exception:
                self._streaming = False
                raise
            self._flush = FLUSH_FRAMES_AFTER_APPLY

    def _exposure_spec(self) -> SettingSpec | None:
        """ExposureTime range; its max assumes the driver may lower the frame rate.

        The camera caps ExposureTime at the current frame period and does not
        lengthen the frame period itself (verified live), so the driver lowers
        AcquisitionFrameRate when a longer exposure is requested (unless the
        frame rate was set explicitly).
        """
        spec = self._num_spec("exposure_us", "ExposureTime", unit="us")
        fps = self._fps_nodes()
        if spec is None or fps is None or self._hdr_on():
            return spec
        fps_min = self._attr(fps[1], "min")
        if not isinstance(fps_min, int | float) or fps_min <= 0:
            return spec
        longest = 1e6 / float(fps_min) - FRAME_OVERHEAD_US
        return spec.model_copy(update={"max": max(spec.max or 0.0, longest)})

    def _apply_exposure(
        self,
        value: float,
        *,
        explicit_fps: bool,
        warnings: list[str],
        node_name: str = "ExposureTime",
        field: str = "exposure_us",
        allow_restore: bool = True,
    ) -> FieldResult:
        node = self._node(node_name)
        if (
            node_name == "ExposureTime"
            and node is not None
            and not self._attr(node, "is_writable")
            and self._hdr_on()
        ):
            return FieldResult(
                field="exposure_us",
                status="rejected",
                requested=value,
                reason="ExposureTime is read-only while HDR mode is on (HDRTuningEnable = True)",
            )
        fps = self._fps_nodes()
        cur_max = self._attr(node, "max") if node is not None else None
        if (
            fps is not None
            and not explicit_fps
            and isinstance(cur_max, int | float)
            and value > cur_max
        ):
            en, rate = fps
            with self._fps_writable(en):
                en.value = True
                lo, hi = float(rate.min), float(rate.max)
                rate.value = min(hi, max(lo, 1e6 / (value + FRAME_OVERHEAD_US)))
                # inside the stopped window: frames after the restart already use it
                result = self._set_number(field, node_name, value)
            self._auto_fps = True
            warnings.append(
                f"frame rate lowered to {float(rate.value):.3f} Hz to allow exposure {value} us"
            )
            return result
        prev = float(node.value) if node is not None else None
        auto = self._auto_fps or self._rate_was_auto(prev)
        result = self._set_number(field, node_name, value)
        if allow_restore and auto and result.status != "rejected":
            self._restore_free_run(value, warnings)
        return result

    def _rate_was_auto(self, exposure: float | None) -> bool:
        """Is the frame rate the one camera_lab lowers to for ``exposure``? A new process (each
        CLI call) does not remember lowering it, but the camera keeps it (fresh-agent test
        2026-10-23: 200 → 150 ms left 4.99 fps). A rate the user chose is left alone."""
        fps = self._fps_nodes()
        if fps is None or exposure is None or not fps[0].value:
            return False
        rate = fps[1]
        auto = min(float(rate.max), max(float(rate.min), 1e6 / (exposure + FRAME_OVERHEAD_US)))
        return abs(float(rate.value) - auto) <= 0.01 * auto

    def _longest_exposure(self) -> float | None:
        """The exposure that sets the frame period: HDR channels with HDR on, else ExposureTime."""
        if self._hdr_on():
            exps = [v for k, v in self._read_hdr().items() if k.endswith(".exposure_us")]
            return max(map(float, exps)) if exps else None
        node = self._node("ExposureTime")
        return float(node.value) if node is not None else None

    def _restore_free_run(self, longest: float | None, warnings: list[str] | None) -> None:
        """Go back to free-run (AcquisitionFrameRateEnable = False) if ``longest`` fits.

        The camera keeps a lowered frame rate, even across close/open (live
        2026-10-21), so the driver restores free-run itself.
        """
        fps = self._fps_nodes()
        if fps is None or longest is None:
            return
        en, rate = fps
        if not en.value:
            self._auto_fps = False
            return
        if longest > 1e6 / float(rate.max) - FRAME_OVERHEAD_US:
            # still needs a lowered rate: the fastest that fits this exposure
            fastest = min(float(rate.max), 1e6 / (longest + FRAME_OVERHEAD_US))
            if float(rate.value) < fastest * 0.999:
                with self._fps_writable(en):
                    rate.value = fastest
                if warnings is not None:
                    warnings.append(
                        f"frame rate raised to {float(rate.value):.3f} Hz (fastest for "
                        f"exposure {longest:.0f} us)"
                    )
            self._auto_fps = True  # restore free-run when it shortens further
            return
        with self._fps_writable(en):
            en.value = False
        self._auto_fps = False
        if warnings is not None:
            warnings.append("frame rate restored to free-run maximum")

    # ------------------------------------------------------ backup / restore
    #: Features the SDK's streamables file leaves out on the TRI054S-C fw 1.138
    #: (verified live 2026-10-21: HDR switches and channels are not streamable;
    #: ExposureTime / Gain are left out while read-only), in write-back order.
    #: (feature, selector or None)
    _SUPPLEMENT: ClassVar[tuple[tuple[str, str | None], ...]] = (
        ("HDRTuningEnable", None),
        ("HDROutput", None),
        ("HDRImageEnhancementEnable", None),
        ("HDRDigitalClampingEnable", None),
        ("HDRChannelExposureTime", "HDRTuningChannelSelector"),
        ("HDRChannelAnalogGain", "HDRTuningChannelSelector"),
        ("ExposureAuto", None),
        ("GainAuto", None),
        ("BalanceWhiteAuto", None),
        ("AcquisitionFrameRateEnable", None),
        ("AcquisitionFrameRate", None),
        ("ExposureTime", None),
        ("Gain", None),
        ("GammaEnable", None),
        ("Gamma", None),
        ("BlackLevel", None),
    )

    def _selector_values(self, selector: str) -> list[str | int]:
        sel = self._node(selector)
        if sel is None:
            return []
        entries = self._attr(sel, "enumentry_names")
        if entries:
            return list(entries)
        lo, hi = self._attr(sel, "min"), self._attr(sel, "max")
        if isinstance(lo, int) and isinstance(hi, int) and hi - lo < 64:
            return list(range(lo, hi + 1))
        return []

    def _read_supplement(self) -> list[FeatureValue]:
        out: list[FeatureValue] = []
        for name, selector in self._SUPPLEMENT:
            if selector is None:
                node = self._readable_node(name)
                if node is not None:
                    out.append(
                        FeatureValue(
                            name=name,
                            value=node.value,
                            writable=bool(self._attr(node, "is_writable")),
                        )
                    )
                continue
            sel = self._node(selector)
            if sel is None or self._readable_node(name) is None:
                continue
            prev = sel.value
            try:
                for v in self._selector_values(selector):
                    sel.value = v
                    node = self._nm.get_node(name)
                    out.append(
                        FeatureValue(
                            name=name,
                            value=node.value,
                            writable=bool(self._attr(node, "is_writable")),
                            selector=selector,
                            selector_value=v,
                        )
                    )
            finally:
                sel.value = prev
        return out

    @contextlib.contextmanager
    def _stream_paused(self) -> Iterator[None]:
        """Pause acquisition around SDK feature persistence (locked while streaming).

        Live TRI054S-C 2026-10-21: write_streamable_node_values_to fails while
        streaming (DeviceFeaturePersistenceStart is not writable).
        """
        if not self._streaming:
            yield
            return
        self._dev.stop_stream()
        try:
            yield
        finally:
            try:
                self._dev.start_stream(STREAM_BUFFERS)
            except Exception:
                self._streaming = False
                raise
            self._flush, self._settle_ns = 0, None  # frames after a restart are fresh

    def _streamables_text(self) -> str:
        with tempfile.TemporaryDirectory(prefix="camera_lab_") as d, self._stream_paused():
            path = Path(d) / "streamables.txt"
            self._nm.write_streamable_node_values_to(str(path))
            return path.read_text(encoding="utf-8", errors="replace")

    def backup_settings(self) -> dict[str, Any]:
        """SDK streamables file + the features it leaves out (``_SUPPLEMENT``)."""
        with self._lock, self._guard():
            return {
                "format": "arena-v1",
                "streamables": self._streamables_text(),
                "supplement": [f.model_dump(mode="json") for f in self._read_supplement()],
            }

    def restore_settings(
        self, payload: dict[str, Any], protected: Callable[[str], bool] | None = None
    ) -> RestoreReport:
        """Load the streamables file, then write the supplement back (multi-pass)."""
        if payload.get("format") != "arena-v1":
            raise ValueError(f"not an Arena backup: {payload.get('format')!r}")
        with self._lock, self._guard():
            if self._streaming:
                raise StreamingStateError("stop the stream before restoring settings")
            failed: list[dict[str, str]] = []
            text = str(payload.get("streamables", ""))
            skipped: list[str] = []
            if protected is not None and text:
                kept = []
                for line in text.splitlines():
                    name, _, value = line.partition("\t")
                    if line.startswith("#") or not protected(name):
                        kept.append(line)
                        continue
                    node = self._readable_node(name)
                    now = (
                        ""
                        if node is None
                        else str(int(node.value) if isinstance(node.value, bool) else node.value)
                    )
                    if now != value:
                        skipped.append(f"{name}: backup {value!r}, camera {now!r}")
                text = "\n".join(kept) + "\n"
            if text:
                with tempfile.TemporaryDirectory(prefix="camera_lab_") as d:
                    path = Path(d) / "streamables.txt"
                    path.write_text(text, encoding="utf-8")
                    try:
                        self._nm.read_streamable_node_values_from(str(path))
                    except Exception as exc:
                        failed.append({"feature": "streamables", "reason": str(exc).strip()[:200]})
            features = [FeatureValue.model_validate(f) for f in payload.get("supplement", [])]
            if protected is not None:
                features = [f for f in features if not protected(f.name)]
            report = multipass_restore(features, _ArenaAccess(self._nm))
            diffs = list(report.differences)
            if text:
                now = self._streamables_text()
                changed = [
                    ln[1:]
                    for ln in difflib.unified_diff(
                        text.splitlines(), now.splitlines(), lineterm="", n=0
                    )
                    if ln[:1] in "+-"
                    and not ln.startswith(("+++", "---"))
                    and not ln[1:].startswith("#")
                    and not ln[1:].startswith(_TRANSPORT_STREAMABLES)
                ]
                diffs += [f"streamables: {c}" for c in changed[:20]]
            self._auto_fps = False
            self._settle_ns = None
            self._flush = 0
            self._settings = self._read_settings()
            return report.model_copy(
                update={
                    "failed": (*failed, *report.failed),
                    "differences": tuple(diffs[:50]),
                    "protected_skipped": tuple(skipped),
                }
            )

    def save_user_set(self, slot: str = "UserSet1", make_default: bool = True) -> dict[str, Any]:
        """UserSetSelector = slot → UserSetSave; UserSetDefault = slot (boot set).

        Live TRI054S-C 2026-10-21: UserSetSelector {Default, UserSet1, UserSet2};
        UserSetSave is not writable while Default is selected; UserSetDefault picks
        the boot set; UserSetFeatureSelector lists what a user set stores (it does
        not include HDRTuningEnable or the HDR channel exposures / gains).
        """
        if not slot.startswith("UserSet"):
            raise ValueError(f"user set must be UserSet1/2/..., not {slot!r}")
        with self._lock, self._guard(), self._stream_paused():
            sel = self._nm.get_node("UserSetSelector")
            if slot not in (self._attr(sel, "enumentry_names") or []):
                raise ValueError(f"camera has no user set {slot!r}")
            sel.value = slot
            self._nm.get_node("UserSetSave").execute()
            default = None
            if make_default:
                node = self._nm.get_node("UserSetDefault")
                node.value = slot
                default = str(node.value)
            feats = self._node("UserSetFeatureSelector")
            stored = list(self._attr(feats, "enumentry_names") or []) if feats is not None else None
            return {"slot": slot, "default": default, "stored_features": stored, "notes": []}

    def reboot(self) -> None:
        """DeviceReset: the camera restarts and reloads its boot user set — identical to a
        power cycle for the settings (live TRI054S-C 2026-10-24: back after 19 s)."""
        with self._lock:
            if self._streaming:
                with contextlib.suppress(Exception):
                    self._dev.stop_stream()
                self._streaming = False
            with contextlib.suppress(Exception):  # the camera drops off while answering
                self._nm.get_node("DeviceReset").execute()

    def uptime_s(self) -> float | None:
        node = self._readable_node("DeviceUpTime")
        return float(node.value) if node is not None else None

    def temperature_c(self) -> float | None:
        with self._lock, self._guard():
            node = self._readable_node("DeviceTemperature")
            return float(node.value) if node is not None else None

    def set_boot_user_set(self, slot: str = "Default") -> dict[str, Any]:
        """UserSetDefault = slot (live TRI054S-C: Default, UserSet1, UserSet2)."""
        with self._lock, self._guard(), self._stream_paused():
            node = self._nm.get_node("UserSetDefault")
            if slot not in (self._attr(node, "enumentry_names") or []):
                raise ValueError(f"camera cannot boot from {slot!r}")
            before = str(node.value)
            node.value = slot
            return {"boot_set": str(node.value), "was": before}

    def load_user_set(self, slot: str = "Default") -> dict[str, Any]:
        """UserSetSelector = slot → UserSetLoad (stream paused), then the open-time setup.

        Live TRI054S-C 2026-10-23: Default = BayerRG8, 30 ms, gain 0, BlackLevel 0, LUT on
        (gamma 0.5), colour transformation on, HDR tuning off, white balance **auto
        (Continuous)**, ratios ≈ 1; HDR channel gains at their factory values.
        """
        with self._lock, self._guard(), self._stream_paused():
            sel = self._nm.get_node("UserSetSelector")
            if slot not in (self._attr(sel, "enumentry_names") or []):
                raise ValueError(f"camera has no user set {slot!r}")
            sel.value = slot
            self._nm.get_node("UserSetLoad").execute()
            # inside the paused window: some of it is stream-locked (GUI live view, 2026-10-24)
            self.configure_on_open()
        return {"slot": slot, "notes": ["open-time setup re-applied (autos off)"]}

    def identity_nodes(self) -> dict[str, str]:
        with self._lock, self._guard():
            out: dict[str, str] = {}
            for name in IDENTITY_NODES:
                n = self._readable_node(name)
                if n is not None:
                    out[name] = str(n.value)
            return out

    def user_set_info(self) -> dict[str, Any]:
        with self._lock, self._guard():
            sel = self._node("UserSetSelector")
            feats = self._node("UserSetFeatureSelector")
            boot = self._node("UserSetDefault")
            return {
                "slots": list(self._attr(sel, "enumentry_names") or []) if sel is not None else [],
                "boot_set_feature": boot is not None,
                "boot_set": str(boot.value) if boot is not None else None,
                "stored_features": list(self._attr(feats, "enumentry_names") or [])
                if feats is not None
                else None,
                "notes": ["feature persistence (backup) needs the stream paused"],
            }

    # ------------------------------------------------------ HDR (advanced)
    #: On/off features (advanced settings "off" / "on").
    _HDR_FLAGS: ClassVar[dict[str, str]] = {
        "hdr.enabled": "HDRTuningEnable",
        "hdr.enhancement": "HDRImageEnhancementEnable",
        "hdr.clamping": "HDRDigitalClampingEnable",
        # TRI054S default: a gamma-0.5 display LUT on (LUTToneMapping GammaPointFive);
        # Lucid's raw path: BayerRG24 with the LUT and colour transformation off
        # (Lucid KB "Using LUCID's TRI054S IMX490 in ArenaView" / "Tonemapping images").
        "lut.enabled": "LUTEnable",
        "color.transformation": "ColorTransformationEnable",
    }
    #: Choice features (advanced settings with the camera's entry names).
    _ENUMS: ClassVar[dict[str, str]] = {
        "hdr.output": "HDROutput",
        "lut.tone_mapping": "LUTToneMapping",
    }
    #: Numeric features: advanced name → (node, unit). BlackLevel is 0 by default on the
    #: TRI054S-C (dark noise clipped at 0); the fingerprint raises it (live range 0–12.5).
    _NUMERIC: ClassVar[dict[str, tuple[str, str]]] = {"black.level": ("BlackLevel", "%")}
    #: Lucid KB: "You must stop image acquisition before changing" these.
    _STREAM_STOP: ClassVar[frozenset[str]] = frozenset({"hdr.output", "lut.enabled"})

    def _check_stream_stop(self, key: str) -> None:
        """Refuse a change the camera ignores while acquiring (it used to pass silently)."""
        if self._streaming and key in self._STREAM_STOP:
            raise StreamingStateError(f"stop the stream before changing {key}")

    def _hdr_channels(self) -> range:
        sel = self._node("HDRTuningChannelSelector")
        if sel is None:
            return range(0)
        return range(int(self._attr(sel, "min") or 0), int(self._attr(sel, "max") or -1) + 1)

    @contextlib.contextmanager
    def _hdr_channel(self, ch: int) -> Iterator[None]:
        sel = self._nm.get_node("HDRTuningChannelSelector")
        prev = sel.value
        sel.value = ch
        try:
            yield
        finally:
            sel.value = prev

    def _hdr_specs(self) -> dict[str, SettingSpec]:
        """HDR settings (live TRI054S-C 2026-10-21; manual HDR Control L2165 lists names only)."""
        specs: dict[str, SettingSpec] = {}
        for key, node in self._HDR_FLAGS.items():
            if self._readable_node(node) is not None:
                specs[key] = SettingSpec(
                    name=key,
                    kind="enum",
                    options=("off", "on"),
                    requires_stream_stop=key in self._STREAM_STOP,
                )
        for key, node in self._ENUMS.items():
            n = self._readable_node(node)
            if n is not None and self._attr(n, "enumentry_names"):
                specs[key] = SettingSpec(
                    name=key,
                    kind="enum",
                    options=tuple(n.enumentry_names),
                    requires_stream_stop=key in self._STREAM_STOP,
                )
        for key, (node, unit) in self._NUMERIC.items():
            if self._writable_node(node) is not None:
                num = self._num_spec(key, node, unit=unit)
                if num is not None:
                    specs[key] = num
        fps = self._fps_nodes()
        fps_min = self._attr(fps[1], "min") if fps is not None else None
        for ch in self._hdr_channels():
            with self._hdr_channel(ch):
                exp = self._num_spec(f"hdr.ch{ch}.exposure_us", "HDRChannelExposureTime", unit="us")
                gain = self._num_spec(f"hdr.ch{ch}.gain_db", "HDRChannelAnalogGain", unit="dB")
            if exp is not None:
                if isinstance(fps_min, int | float) and fps_min > 0:  # frame rate may be lowered
                    longest = 1e6 / float(fps_min) - FRAME_OVERHEAD_US
                    exp = exp.model_copy(update={"max": max(exp.max or 0.0, longest)})
                specs[exp.name] = exp
            if gain is not None:
                specs[gain.name] = gain
        return specs

    def _read_hdr(self) -> dict[str, float | str]:
        out: dict[str, float | str] = {}
        for key, node in self._HDR_FLAGS.items():
            n = self._node(node)
            if n is not None and self._attr(n, "is_readable"):
                out[key] = "on" if n.value else "off"
        for key, node in self._ENUMS.items():
            n = self._readable_node(node)
            if n is not None:
                out[key] = str(n.value)
        for key, (node, _unit) in self._NUMERIC.items():
            n = self._writable_node(node)
            if n is not None:
                out[key] = float(n.value)
        for ch in self._hdr_channels():
            with self._hdr_channel(ch):
                for suffix, node in (
                    ("exposure_us", "HDRChannelExposureTime"),
                    ("gain_db", "HDRChannelAnalogGain"),
                ):
                    n = self._node(node)
                    if n is not None and self._attr(n, "is_readable"):
                        out[f"hdr.ch{ch}.{suffix}"] = float(n.value)
        return out

    def _apply_hdr(
        self, adv: dict[str, Any], *, explicit_fps: bool, warnings: list[str]
    ) -> list[FieldResult]:
        """Apply HDR advanced settings: switches and output first, then channel values."""
        results: list[FieldResult] = []
        ordered = sorted(adv.items(), key=lambda kv: (".ch" in kv[0], kv[0]))
        for key, value in ordered:
            field = f"advanced.{key}"
            try:
                if key in self._HDR_FLAGS:
                    if value not in ("off", "on"):
                        raise ValueError(f"{key} must be 'off' or 'on'")
                    node = self._nm.get_node(self._HDR_FLAGS[key])
                    if bool(node.value) != (value == "on"):
                        self._check_stream_stop(key)
                        node.value = value == "on"
                    results.append(
                        FieldResult(
                            field=field,
                            status="applied",
                            requested=value,
                            value="on" if node.value else "off",
                        )
                    )
                elif key in self._NUMERIC:
                    results.append(self._set_number(field, self._NUMERIC[key][0], float(value)))
                elif key in self._ENUMS:
                    node = self._nm.get_node(self._ENUMS[key])
                    if node.value != value:
                        self._check_stream_stop(key)
                        node.value = value
                    results.append(
                        FieldResult(
                            field=field, status="applied", requested=value, value=str(node.value)
                        )
                    )
                elif m := re.fullmatch(r"hdr\.ch(\d+)\.(exposure_us|gain_db)", key):
                    ch, kind = int(m.group(1)), m.group(2)
                    if ch not in self._hdr_channels():
                        raise ValueError(f"no HDR channel {ch}")
                    with self._hdr_channel(ch):
                        if kind == "exposure_us":
                            results.append(
                                self._apply_exposure(
                                    float(value),
                                    explicit_fps=explicit_fps,
                                    warnings=warnings,
                                    node_name="HDRChannelExposureTime",
                                    field=field,
                                    allow_restore=False,
                                )
                            )
                        else:
                            results.append(
                                self._set_number(field, "HDRChannelAnalogGain", float(value))
                            )
                else:
                    raise ValueError(f"advanced setting {key!r} not supported")
            except Exception as exc:
                results.append(
                    FieldResult(
                        field=field,
                        status="rejected",
                        requested=value,
                        reason=str(exc).strip()[:200],
                    )
                )
        channel_exposure = any(k.startswith("hdr.ch") and k.endswith("exposure_us") for k in adv)
        if channel_exposure and self._auto_fps and not explicit_fps:
            self._restore_free_run(self._longest_exposure(), warnings)
        return results

    def _apply_gain(self, value: float) -> FieldResult:
        node = self._node("Gain")
        if node is not None and not self._attr(node, "is_writable") and self._hdr_on():
            return FieldResult(
                field="gain_db",
                status="rejected",
                requested=value,
                reason="Gain is read-only while HDR mode is on (HDRTuningEnable = True); "
                "tune the HDR channel gains instead",
            )
        return self._set_number("gain_db", "Gain", value)

    def _apply_wb(self, wb: dict[WbChannel, float]) -> list[FieldResult]:
        sel = self._readable_node("BalanceRatioSelector")
        if sel is None or self._readable_node("BalanceRatio") is None:
            return [
                FieldResult(
                    field=f"white_balance.{ch}",
                    status="rejected",
                    requested=v,
                    reason="white balance not supported (mono camera?)",
                )
                for ch, v in wb.items()
            ]
        prev = sel.value
        out = []
        try:
            for ch, v in wb.items():
                sel.value = _WB_ENTRIES[ch]
                out.append(self._set_number(f"white_balance.{ch}", "BalanceRatio", v))
        finally:
            sel.value = prev
        return out

    def _apply_trigger(self, mode: str) -> FieldResult:
        try:
            if mode == "software":
                self._nm.get_node("TriggerSelector").value = "FrameStart"
                self._nm.get_node("TriggerSource").value = "Software"
                self._nm.get_node("TriggerMode").value = "On"
            else:
                self._nm.get_node("TriggerMode").value = "Off"
        except Exception as exc:
            return FieldResult(
                field="trigger_mode",
                status="rejected",
                requested=mode,
                reason=f"trigger: {str(exc).strip()[:200]}",
            )
        return FieldResult(field="trigger_mode", status="applied", requested=mode, value=mode)

    # ------------------------------------------------------------- stream
    def start_stream(self) -> None:
        """Start acquisition (idempotent)."""
        with self._lock, self._guard():
            if not self._streaming:
                self._dev.start_stream(STREAM_BUFFERS)
                self._streaming = True
                self._flush = 0
                self._settle_ns = None

    def stop_stream(self) -> None:
        """Stop acquisition (idempotent)."""
        with self._lock:
            if self._streaming:
                try:
                    self._dev.stop_stream()
                finally:
                    self._streaming = False

    def _get_buffer(self, timeout_ms: int) -> Any:
        try:
            return self._dev.get_buffer(timeout=int(timeout_ms))
        except TimeoutError:
            if not self._connected():
                raise CameraDisconnectedError(f"camera {self._info.serial} disconnected") from None
            raise CaptureTimeoutError(
                f"no frame from {self._info.serial} within {timeout_ms} ms"
            ) from None

    def _software_trigger(self, timeout_ms: int) -> None:
        armed = self._node("TriggerArmed")
        deadline = time.monotonic() + timeout_ms / 1000
        while armed is not None and not bool(armed.value):
            if time.monotonic() > deadline:
                raise CaptureTimeoutError(f"trigger not armed within {timeout_ms} ms")
            time.sleep(TRIGGER_ARM_POLL_S)
        self._nm.get_node("TriggerSoftware").execute()

    def grab(self, timeout_ms: int = 2000) -> Frame:
        """Return the next frame as a copy; the buffer is always requeued."""
        with self._lock, self._guard():
            if timeout_ms <= 0:
                raise ValueError("timeout_ms must be > 0")
            if not self._streaming:
                raise StreamingStateError("camera is not streaming; call start_stream() first")
            settings = self._settings or self._read_settings()
            software = settings.trigger_mode == "software"
            if not software:
                while self._flush > 0:
                    stale = self._get_buffer(timeout_ms)
                    self._dev.requeue_buffer(stale)
                    self._flush -= 1
                skipped = 0
                while self._settle_ns is not None and skipped < MAX_STALE_FRAMES:
                    buf = self._get_buffer(timeout_ms)
                    try:
                        fresh = int(buf.timestamp_ns) >= self._settle_ns
                    except BaseException:
                        self._dev.requeue_buffer(buf)
                        raise
                    if fresh:
                        self._settle_ns = None
                        return self._to_frame(buf, settings)
                    self._dev.requeue_buffer(buf)
                    skipped += 1
                self._settle_ns = None
            else:
                self._flush = 0
                self._software_trigger(timeout_ms)
            return self._to_frame(self._get_buffer(timeout_ms), settings)

    def _to_frame(self, buf: Any, settings: CameraSettings) -> Frame:
        """Copy ``buf`` into a Frame and requeue it (always)."""
        try:
            if buf.is_incomplete:
                raise IncompleteFrameError(f"incomplete frame from {self._info.serial}")
            data = buffer_to_array(buf, settings.pixel_format)
            frame_id = int(buf.frame_id)
        finally:
            self._dev.requeue_buffer(buf)
        return Frame(
            data=data,
            pixel_format=settings.pixel_format,
            bit_depth=PIXEL_FORMAT_BITS[settings.pixel_format],
            timestamp_utc=self._clock(),
            frame_id=frame_id,
            settings=settings.model_copy(deep=True),
        )

    def close(self) -> None:
        """Stop streaming and destroy the device (always)."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                if self._streaming:
                    try:
                        self._dev.stop_stream()
                    except Exception as exc:
                        _log.warning("arena_stop_stream_failed", error=str(exc))
                    self._streaming = False
            finally:
                self._system.destroy_device(self._dev)
                _log.info("arena_device_destroyed", camera_id=self._info.serial)


class _ArenaAccess(FeatureAccess):
    """Arena node map access for :func:`multipass_restore`."""

    def __init__(self, nm: Any) -> None:
        self._nm = nm

    def select(self, selector: str, value: str | int) -> None:
        node = self._nm.get_node(selector)
        if node.value != value:
            node.value = value

    def read(self, name: str) -> Any:
        return self._nm.get_node(name).value

    def writable(self, name: str) -> bool:
        return bool(self._nm.get_node(name).is_writable)

    def write(self, name: str, value: Any) -> None:
        self._nm.get_node(name).value = value


#: Streamable nodes the GigE transport sets itself when a stream starts (live TRI054S-C
#: 2026-10-23: GevSCPSDoNotFragment flips 0 → 1): not a camera setting, not a restore failure.
_TRANSPORT_STREAMABLES = ("GevSCPSDoNotFragment",)


def buffer_to_array(buf: Any, pixel_format: str) -> npt.NDArray[Any]:
    """Copy an Arena buffer into a numpy array (unpacking *10p/*12p to uint16)."""
    w, h = int(buf.width), int(buf.height)
    n = int(buf.payload_size)
    ch = _channels(pixel_format)
    ptr = ctypes.cast(buf.pdata, ctypes.POINTER(ctypes.c_ubyte))
    raw = np.ctypeslib.as_array(ptr, shape=(n,)).copy()  # copy before requeue
    count = w * h * ch
    shape = (h, w, 3) if ch == 3 else (h, w)
    if pixel_format.endswith("24"):
        return unpack_24(raw, count).reshape(shape)
    if pixel_format.endswith("12p"):
        return unpack_12p(raw, count).reshape(shape)
    if pixel_format.endswith("10p"):
        return unpack_10p(raw, count).reshape(shape)
    bits = PIXEL_FORMAT_BITS.get(pixel_format)
    if bits is None:
        raise ValueError(f"unsupported pixel format {pixel_format!r}")
    nbytes = 1 if bits <= 8 else 2
    if n < count * nbytes:
        raise IncompleteFrameError(f"payload {n} bytes < expected {count * nbytes}")
    if nbytes == 1:
        return raw[:count].reshape(shape)
    return raw[: count * 2].view("<u2").astype(np.uint16).reshape(shape)


class ArenaDriver(Camera2DDriver):
    """Discovers and opens Lucid cameras through Arena (skips every other vendor)."""

    name: ClassVar[str] = "arena"

    def __init__(
        self,
        system: Any = None,
        *,
        clock: Callable[[], datetime] = _utc_now,
        discovery_timeout_ms: int = DISCOVERY_TIMEOUT_MS,
    ) -> None:
        self._system = system if system is not None else _load_system()
        self._clock = clock
        self.sdk_version = _sdk_version() if system is None else "fake"
        with contextlib.suppress(Exception):
            self._system.DEVICE_INFOS_TIMEOUT_MILLISEC = int(discovery_timeout_ms)

    def _lucid_infos(self) -> list[dict[str, Any]]:
        infos = list(self._system.device_infos)
        return [i for i in infos if str(i.get("vendor", "")).strip().casefold() == LUCID_VENDOR]

    @staticmethod
    def _to_info(d: dict[str, Any]) -> CameraInfo:
        return CameraInfo(
            vendor=str(d.get("vendor", "")),
            model=str(d.get("model", "")),
            serial=str(d.get("serial", "")),
            firmware=str(d.get("version")) if d.get("version") else None,
            interface="GigE",
            ip=str(d.get("ip")) if d.get("ip") else None,
            driver="arena",
        )

    def discover(self) -> list[CameraInfo]:
        """List Lucid cameras from ``system.device_infos`` (no device is created)."""
        return [self._to_info(d) for d in self._lucid_infos()]

    def open(self, camera_id: str) -> ArenaCamera:
        """Create exactly one device (this Lucid serial) and configure it."""
        match = [d for d in self._lucid_infos() if str(d.get("serial")) == camera_id]
        if not match:
            raise CameraNotFoundError(f"no Lucid camera with serial {camera_id!r}")
        devices = self._system.create_device(match[0])
        device = devices[0] if isinstance(devices, list) else devices
        cam = ArenaCamera(self._system, device, self._to_info(match[0]), self._clock)
        try:
            changes = cam.open_changes_needed()
            if changes:  # back up before camera_lab changes anything (Derek, 2026-10-21)
                cam.open_backup = {**cam.backup_settings(), "open_changes": changes}
            cam.configure_on_open()
        except BaseException:
            cam.close()
            raise
        _log.info("arena_device_opened", camera_id=camera_id)
        return cam
