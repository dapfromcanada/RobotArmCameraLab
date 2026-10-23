"""Simulated 2D camera: deterministic, physically plausible, no hardware needed.

Scene (reflectance 0–1, fixed, no randomness): a 10-step grey chart (top), six
colour patches (middle), a 16 px checkerboard and a slanted black/white edge
(bottom), on a 40 % grey background. The brightest reflectance (0.85) is
about 2x the scene mean, so a mean level of 0.45 is reachable without clipping.

Image formation, per pixel and colour channel::

    electrons = reflectance × (exposure_us / 10 000) × light_factor × 10 000 e⁻
    noisy     = Poisson(min(electrons, full well)) + Normal(0, 5 e⁻ read noise)
    level     = noisy / 10 000 × 10^(gain_db / 20) × wb[channel]
    level     = clip(level, 0, 1) ** gamma   (GenICam: gamma > 1 darkens)
    code      = round(level × (2^bit_depth − 1))

``light_factor = ambient + SimulatedLight.light_factor()``. The ``ambient``
term (default 0.2) keeps the sim usable with all lights off; set it to 0 for a
pure ``light_factor`` model. Optional ``black_level`` adds a constant pedestal
(fraction of full scale; the JAI shows ~0.03) and ``hot_spot_pct`` adds a tiny,
very bright highlight that clips early (a lamp in view). Noise is seeded by
``(seed, frame_id)`` so every run is reproducible. ``defocus_sigma`` (px) blurs
the scene for sharpness tests.

Sensor model (Phase 11, :class:`SensorModel`, defaults = the values above): full
well, read noise, dark current (e⁻/s, plus ``hot_pixels`` with 1000× dark
current), pixel response non-uniformity ``prnu`` (σ of a fixed per-pixel gain)
and ``vignetting`` (fractional fall-off at the corners, ``1 − v·r²``). The
camera's ``scene`` switches between the chart (``"chart"``), a lens cap
(``"cap"``: no light) and an even diffuser (``"flat"``: reflectance 0.8), so the
fingerprint can be tested against known values.

Formats: Mono8, Mono12 (uint16, native 0–4095), BayerRG8 (RGGB mosaic), RGB8.
Changing the pixel format or the ROI size while streaming raises
:class:`StreamingStateError`, like a real camera with ``TLParamsLocked``.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, ClassVar, Literal

import cv2
import numpy as np
import numpy.typing as npt

from camera_lab.core.lights.simulated import SimulatedLight
from camera_lab.core.safety import Accepted, Clamped, Rejected, check, effective_range
from camera_lab.two_d.backups import RestoreReport
from camera_lab.two_d.drivers.base import Camera2D, Camera2DDriver
from camera_lab.two_d.errors import (
    CameraClosedError,
    CameraNotFoundError,
    CaptureTimeoutError,
    IncompleteFrameError,
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
)

SIM_SERIAL = "SIM-0001"
SIM_FORMATS = ("Mono8", "Mono12", "BayerRG8", "RGB8")
SENSOR_WIDTH = 1024
SENSOR_HEIGHT = 768
EXPOSURE_REF_US = 10_000.0
FULL_WELL_E = 10_000.0
READ_NOISE_E = 5.0
FLAT_REFLECTANCE = 0.8
HOT_PIXEL_FACTOR = 1000.0
SceneKind = Literal["chart", "cap", "flat"]


@dataclass(frozen=True)
class SensorModel:
    """Known sensor properties of the simulated camera (fingerprint tests)."""

    full_well_e: float = FULL_WELL_E
    read_noise_e: float = READ_NOISE_E
    dark_current_e_s: float = 0.0
    prnu: float = 0.0
    vignetting: float = 0.0
    hot_pixels: int = 0


_REC709 = np.array([0.2126, 0.7152, 0.0722])


def _utc_now() -> datetime:
    return datetime.now(UTC)


def build_scene(
    width: int = SENSOR_WIDTH, height: int = SENSOR_HEIGHT, hot_spot_pct: float = 0.0
) -> npt.NDArray[np.float64]:
    """Return the fixed RGB reflectance scene, shape (height, width, 3), values 0.05–0.85."""
    scene = np.full((height, width, 3), 0.40)
    # grey step chart
    y0, y1 = int(0.05 * height), int(0.25 * height)
    steps = np.linspace(0.05, 0.85, 10)
    edges = np.linspace(0, width, 11).astype(int)
    for i, refl in enumerate(steps):
        scene[y0:y1, edges[i] : edges[i + 1]] = refl
    # colour patches
    colours = [
        (0.70, 0.10, 0.10),
        (0.10, 0.60, 0.15),
        (0.10, 0.15, 0.65),
        (0.75, 0.70, 0.10),
        (0.10, 0.60, 0.65),
        (0.65, 0.15, 0.60),
    ]
    y0, y1 = int(0.32 * height), int(0.52 * height)
    pw = width // 6
    for i, c in enumerate(colours):
        scene[y0:y1, i * pw + pw // 8 : (i + 1) * pw - pw // 8] = c
    # checkerboard (left) and slanted edge (right)
    y0, y1 = int(0.60 * height), int(0.95 * height)
    yy, xx = np.mgrid[y0:y1, 0 : width // 2]
    board = ((yy // 16 + xx // 16) % 2).astype(float) * 0.70 + 0.10
    scene[y0:y1, 0 : width // 2] = board[..., None]
    yy, xx = np.mgrid[y0:y1, width // 2 : width]
    edge_x = width * 0.75 + (yy - (y0 + y1) / 2) * math.tan(math.radians(5))
    scene[y0:y1, width // 2 : width] = np.where(xx < edge_x, 0.10, 0.80)[..., None]
    if hot_spot_pct > 0:  # a small, very bright highlight (lamp / specular) in the top-right
        side = max(1, round(math.sqrt(hot_spot_pct / 100.0 * width * height)))
        scene[:side, width - side :] = 8.0
    return scene


class SimulatedCamera(Camera2D):
    """The open simulated camera."""

    def __init__(
        self,
        info: CameraInfo,
        *,
        light: SimulatedLight | None,
        seed: int,
        ambient: float,
        defocus_sigma: float,
        black_level: float = 0.0,
        hot_spot_pct: float = 0.0,
        clock: Callable[[], datetime],
        width: int,
        height: int,
        sensor: SensorModel | None = None,
    ) -> None:
        self._info = info
        self.sensor = sensor or SensorModel()
        self.scene: SceneKind = "chart"
        self._light = light
        self._seed = seed
        self.ambient = ambient
        self.defocus_sigma = defocus_sigma
        self.black_level = black_level
        self._clock = clock
        self._w, self._h = width, height
        self._scene = build_scene(width, height, hot_spot_pct)
        self._blur_cache: dict[float, npt.NDArray[np.float64]] = {}
        self._pixel_gain, self._dark_factor = self._sensor_maps()
        self._settings = CameraSettings(
            exposure_us=5000.0,
            gain_db=0.0,
            gamma=1.0,
            white_balance={"red": 1.0, "green": 1.0, "blue": 1.0},
            pixel_format="BayerRG8",
            roi=ROI(x=0, y=0, width=width, height=height),
            frame_rate_hz=10.0,
            trigger_mode="off",
            advanced={
                "hdr.enabled": "off",
                "hdr.ch0.exposure_us": 5000.0,
                "hdr.ch1.exposure_us": 5000.0,
                "prism.exposure_mode": "common",
                "prism.exposure_red_us": 5000.0,
                "prism.exposure_green_us": 5000.0,
                "prism.exposure_blue_us": 5000.0,
            },
        )
        self._factory = self._settings  # what "Default" loads
        self._streaming = False
        self._closed = False
        self._frame_id = 0
        self._fault: str | None = None

    # ------------------------------------------------------------ properties
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

    # ---------------------------------------------------------- capabilities
    def capabilities(self) -> dict[str, SettingSpec]:
        """Return the simulated camera's ranges (wider than the default safety limits)."""
        self._check_open()
        w, h = self._w, self._h
        specs = [
            SettingSpec(
                name="exposure_us", kind="float", min=10, max=1_000_000, increment=1, unit="us"
            ),
            SettingSpec(name="gain_db", kind="float", min=0, max=24, increment=0.1, unit="dB"),
            SettingSpec(name="gamma", kind="float", min=0.2, max=4.0, increment=0.01),
            *(
                SettingSpec(name=f"wb_{c}", kind="float", min=0.5, max=4.0, increment=0.01)
                for c in ("red", "green", "blue")
            ),
            SettingSpec(
                name="pixel_format", kind="enum", options=SIM_FORMATS, requires_stream_stop=True
            ),
            SettingSpec(
                name="roi_x",
                kind="int",
                min=0,
                max=w - 16,
                increment=2,
                unit="px",
                affects_calibration=True,
            ),
            SettingSpec(
                name="roi_y",
                kind="int",
                min=0,
                max=h - 16,
                increment=2,
                unit="px",
                affects_calibration=True,
            ),
            SettingSpec(
                name="roi_width",
                kind="int",
                min=16,
                max=w,
                increment=16,
                unit="px",
                affects_calibration=True,
                requires_stream_stop=True,
            ),
            SettingSpec(
                name="roi_height",
                kind="int",
                min=16,
                max=h,
                increment=2,
                unit="px",
                affects_calibration=True,
                requires_stream_stop=True,
            ),
            SettingSpec(
                name="frame_rate_hz", kind="float", min=1, max=60, increment=0.1, unit="Hz"
            ),
            SettingSpec(name="trigger_mode", kind="enum", options=("off", "software")),
            # advanced: an HDR-like mode (Triton-style) and a prism-like mode (JAI-style)
            SettingSpec(name="hdr.enabled", kind="enum", options=("off", "on")),
            *(
                SettingSpec(
                    name=f"hdr.ch{i}.exposure_us",
                    kind="float",
                    min=10,
                    max=1_000_000,
                    increment=1,
                    unit="us",
                )
                for i in (0, 1)
            ),
            SettingSpec(name="prism.exposure_mode", kind="enum", options=("common", "individual")),
            *(
                SettingSpec(
                    name=f"prism.exposure_{c}_us",
                    kind="float",
                    min=10,
                    max=1_000_000,
                    increment=1,
                    unit="us",
                )
                for c in ("red", "green", "blue")
            ),
        ]
        return {s.name: s for s in specs}

    def get_settings(self) -> CameraSettings:
        """Return the current settings (the sim's 'read back')."""
        self._check_open()
        return self._settings.model_copy(deep=True)

    # ----------------------------------------------------------------- apply
    def _check_number(
        self, field: str, spec: SettingSpec, value: Any
    ) -> tuple[FieldResult, float | None]:
        rng = effective_range(spec.name, spec.camera_range(), None, None)
        res = check(value, rng, mode="reject")
        if isinstance(res, Rejected):
            return FieldResult(
                field=field, status="rejected", requested=value, reason=res.reason
            ), None
        v = float(res.value)
        if spec.kind == "int":
            v = float(round(v))
        if isinstance(res, Clamped):
            return FieldResult(
                field=field, status="clamped", requested=value, value=v, reason=res.reason
            ), v
        assert isinstance(res, Accepted)  # noqa: S101 - exhaustive
        return FieldResult(field=field, status="applied", requested=value, value=v), v

    def apply(self, patch: CameraSettingsPatch) -> ApplyResult:
        """Validate against the camera's own ranges, apply, and read back."""
        self._check_open()
        caps = self.capabilities()
        cur = self._settings
        if self._streaming:
            blocked = []
            if patch.pixel_format is not None and patch.pixel_format != cur.pixel_format:
                blocked.append("pixel_format")
            if patch.roi is not None and (
                patch.roi.width != cur.roi.width or patch.roi.height != cur.roi.height
            ):
                blocked.append("roi size")
            if blocked:
                raise StreamingStateError(f"stop the stream before changing {', '.join(blocked)}")
        results: list[FieldResult] = []
        new: dict[str, Any] = {}
        for name in ("exposure_us", "gain_db", "gamma", "frame_rate_hz"):
            value = getattr(patch, name)
            if value is None:
                continue
            fr, v = self._check_number(name, caps[name], value)
            results.append(fr)
            if v is not None:
                new[name] = v
        if patch.white_balance is not None:
            wb = dict(cur.white_balance or {})
            for ch, value in patch.white_balance.items():
                fr, v = self._check_number(f"white_balance.{ch}", caps[f"wb_{ch}"], value)
                results.append(fr)
                if v is not None:
                    wb[ch] = v
            new["white_balance"] = wb
        if patch.pixel_format is not None:
            if patch.pixel_format in caps["pixel_format"].options:
                results.append(
                    FieldResult(
                        field="pixel_format",
                        status="applied",
                        requested=patch.pixel_format,
                        value=patch.pixel_format,
                    )
                )
                new["pixel_format"] = patch.pixel_format
            else:
                results.append(
                    FieldResult(
                        field="pixel_format",
                        status="rejected",
                        requested=patch.pixel_format,
                        reason=f"pixel_format {patch.pixel_format!r} not supported by camera"
                        f" ({', '.join(caps['pixel_format'].options)})",
                    )
                )
        if patch.trigger_mode is not None:
            results.append(
                FieldResult(
                    field="trigger_mode",
                    status="applied",
                    requested=patch.trigger_mode,
                    value=patch.trigger_mode,
                )
            )
            new["trigger_mode"] = patch.trigger_mode
        if patch.roi is not None:
            fr_roi, roi = self._check_roi(patch.roi, caps)
            results.append(fr_roi)
            if roi is not None:
                new["roi"] = roi
        if patch.advanced is not None:
            adv = dict(cur.advanced)
            for key, value in patch.advanced.items():
                field = f"advanced.{key}"
                spec = caps.get(key)
                if spec is None:
                    results.append(
                        FieldResult(
                            field=field,
                            status="rejected",
                            requested=value,
                            reason=f"{key} not supported by camera",
                        )
                    )
                elif spec.kind == "enum":
                    ok = value in spec.options
                    results.append(
                        FieldResult(
                            field=field,
                            status="applied" if ok else "rejected",
                            requested=value,
                            value=value if ok else None,
                            reason=None if ok else f"{key} {value!r} not in {spec.options}",
                        )
                    )
                    if ok:
                        adv[key] = value
                else:
                    fr, v = self._check_number(field, spec, value)
                    results.append(fr)
                    if v is not None:
                        adv[key] = v
            new["advanced"] = adv
        self._settings = cur.model_copy(update=new)
        return ApplyResult(results=tuple(results), settings=self.get_settings())

    def _check_roi(self, roi: ROI, caps: dict[str, SettingSpec]) -> tuple[FieldResult, ROI | None]:
        vals: dict[str, int] = {}
        reasons: list[str] = []
        for part in ("x", "y", "width", "height"):
            fr, v = self._check_number(f"roi.{part}", caps[f"roi_{part}"], getattr(roi, part))
            if v is None:
                return FieldResult(
                    field="roi", status="rejected", requested=roi.model_dump(), reason=fr.reason
                ), None
            vals[part] = int(v)
            if fr.status == "clamped" and fr.reason:
                reasons.append(fr.reason)
        if vals["x"] + vals["width"] > self._w or vals["y"] + vals["height"] > self._h:
            return FieldResult(
                field="roi",
                status="rejected",
                requested=roi.model_dump(),
                reason=f"roi {vals} exceeds sensor {self._w}x{self._h}",
            ), None
        new = ROI(**vals)
        status: Literal["applied", "clamped"] = "clamped" if reasons else "applied"
        return FieldResult(
            field="roi",
            status=status,
            requested=roi.model_dump(),
            value=new.model_dump(),
            reason="; ".join(reasons) or None,
        ), new

    # --------------------------------------------------------------- stream
    def start_stream(self) -> None:
        """Start acquisition."""
        self._check_open()
        self._streaming = True

    def stop_stream(self) -> None:
        """Stop acquisition."""
        self._streaming = False

    def inject_fault(self, kind: str | None) -> None:
        """Make the next grab fail: ``"timeout"`` or ``"incomplete"`` (tests only)."""
        if kind not in (None, "timeout", "incomplete"):
            raise ValueError(f"unknown fault {kind!r}")
        self._fault = kind

    def light_factor(self) -> float:
        """``ambient + SimulatedLight.light_factor()``."""
        return self.ambient + (self._light.light_factor() if self._light is not None else 0.0)

    def _sensor_maps(self) -> tuple[npt.NDArray[np.float64], npt.NDArray[np.float64]]:
        """Fixed per-pixel gain (PRNU × vignetting) and dark-current factor (hot pixels)."""
        h, w, sm = self._h, self._w, self.sensor
        rng = np.random.default_rng([self._seed, 999_983])
        gain = np.ones((h, w)) if sm.prnu <= 0 else rng.normal(1.0, sm.prnu, (h, w))
        if sm.vignetting > 0:
            yy, xx = np.mgrid[0:h, 0:w]
            r2 = ((xx - (w - 1) / 2) / ((w - 1) / 2)) ** 2 + (
                (yy - (h - 1) / 2) / ((h - 1) / 2)
            ) ** 2
            gain = gain * (1.0 - sm.vignetting * r2 / 2.0)  # r² = 2 at the corners
        dark = np.ones((h, w))
        if sm.hot_pixels > 0:
            idx = rng.choice(h * w, size=sm.hot_pixels, replace=False)
            dark.reshape(-1)[idx] = HOT_PIXEL_FACTOR
        return gain, dark

    def _reflectance(self) -> npt.NDArray[np.float64]:
        if self.scene == "cap":
            return np.zeros_like(self._scene)
        if self.scene == "flat":
            return np.full_like(self._scene, FLAT_REFLECTANCE)
        sigma = float(self.defocus_sigma)
        if sigma <= 0:
            return self._scene
        if sigma not in self._blur_cache:
            self._blur_cache[sigma] = np.asarray(
                cv2.GaussianBlur(self._scene, (0, 0), sigmaX=sigma), dtype=np.float64
            )
        return self._blur_cache[sigma]

    def grab(self, timeout_ms: int = 2000) -> Frame:
        """Render the next frame."""
        self._check_open()
        if timeout_ms <= 0:
            raise ValueError("timeout_ms must be > 0")
        if not self._streaming:
            raise StreamingStateError("camera is not streaming; call start_stream() first")
        fault, self._fault = self._fault, None
        if fault == "timeout":
            raise CaptureTimeoutError(f"no frame from {self._info.serial} within {timeout_ms} ms")
        if fault == "incomplete":
            raise IncompleteFrameError(f"incomplete frame from {self._info.serial}")
        self._frame_id += 1
        s = self._settings
        data = self.render(s, self._frame_id)
        return Frame(
            data=data,
            pixel_format=s.pixel_format,
            bit_depth=PIXEL_FORMAT_BITS[s.pixel_format],
            timestamp_utc=self._clock(),
            frame_id=self._frame_id,
            settings=s.model_copy(deep=True),
        )

    def render(self, s: CameraSettings, frame_id: int) -> npt.NDArray[Any]:
        """Render one frame for settings ``s`` (deterministic for ``(seed, frame_id)``)."""
        r = s.roi
        refl = self._reflectance()[r.y : r.y + r.height, r.x : r.x + r.width]
        fmt = s.pixel_format
        if fmt.startswith("Mono"):
            refl = refl @ _REC709
        elif fmt == "BayerRG8":
            mosaic = np.empty(refl.shape[:2])
            mosaic[0::2, 0::2] = refl[0::2, 0::2, 0]  # R
            mosaic[0::2, 1::2] = refl[0::2, 1::2, 1]  # G
            mosaic[1::2, 0::2] = refl[1::2, 0::2, 1]  # G
            mosaic[1::2, 1::2] = refl[1::2, 1::2, 2]  # B
            refl = mosaic
        refl = np.asarray(refl, dtype=np.float64)
        rng = np.random.default_rng([self._seed, frame_id])
        adv = s.advanced
        hdr = adv.get("hdr.enabled") == "on"
        exposure: Any = (
            float(adv.get("hdr.ch0.exposure_us", s.exposure_us)) if hdr else s.exposure_us
        )
        if fmt == "RGB8" and not hdr and adv.get("prism.exposure_mode") == "individual":
            exposure = np.array(
                [float(adv[f"prism.exposure_{c}_us"]) for c in ("red", "green", "blue")]
            )
        sm = self.sensor
        pix = self._pixel_gain[r.y : r.y + r.height, r.x : r.x + r.width]
        dark = self._dark_factor[r.y : r.y + r.height, r.x : r.x + r.width]
        if refl.ndim == 3:
            pix, dark = pix[..., None], dark[..., None]
        electrons = refl * (exposure / EXPOSURE_REF_US) * self.light_factor() * FULL_WELL_E * pix
        electrons = electrons + sm.dark_current_e_s * (np.asarray(exposure) / 1e6) * dark
        electrons = np.minimum(electrons, sm.full_well_e * (4.0 if hdr else 1.0))
        noisy = rng.poisson(np.maximum(electrons, 0.0)).astype(np.float64)
        noisy += rng.normal(0.0, sm.read_noise_e, size=noisy.shape)
        level = noisy / sm.full_well_e * 10 ** (s.gain_db / 20.0)
        wb = s.white_balance or {"red": 1.0, "green": 1.0, "blue": 1.0}
        if fmt == "RGB8":
            level = level * np.array([wb["red"], wb["green"], wb["blue"]])
        elif fmt == "BayerRG8":
            level[0::2, 0::2] *= wb["red"]
            level[0::2, 1::2] *= wb["green"]
            level[1::2, 0::2] *= wb["green"]
            level[1::2, 1::2] *= wb["blue"]
        if hdr:  # compressive tone curve: highlights roll off instead of clipping
            level = 1.0 - np.exp(-2.5 * level)
        level = np.clip(level + self.black_level, 0.0, 1.0)  # sensor pedestal (JAI: ~0.03)
        gamma = s.gamma or 1.0
        if gamma != 1.0:
            level = level**gamma  # GenICam / Lucid manual: X = Y^Gamma
        bits = PIXEL_FORMAT_BITS[fmt]
        dtype = np.uint8 if bits <= 8 else np.uint16
        out: npt.NDArray[Any] = np.rint(level * ((1 << bits) - 1)).astype(dtype)
        return out

    # ------------------------------------------------------ backup / restore
    def backup_settings(self) -> dict[str, Any]:
        """The simulator's whole state is its settings."""
        return {"format": "simulated-v1", "settings": self.get_settings().model_dump(mode="json")}

    def restore_settings(
        self, payload: dict[str, Any], protected: Callable[[str], bool] | None = None
    ) -> RestoreReport:
        if payload.get("format") != "simulated-v1":
            raise ValueError(f"not a simulated-camera backup: {payload.get('format')!r}")
        want = CameraSettings.model_validate(payload["settings"])
        res = self.apply(
            CameraSettingsPatch(
                exposure_us=want.exposure_us,
                gain_db=want.gain_db,
                gamma=want.gamma,
                white_balance=want.white_balance,
                pixel_format=want.pixel_format,
                roi=want.roi,
                frame_rate_hz=want.frame_rate_hz,
                trigger_mode=want.trigger_mode,
                advanced=dict(want.advanced) or None,
            )
        )
        failed = tuple(
            {"feature": r.field, "reason": r.reason or r.status}
            for r in res.results
            if r.status == "rejected"
        )
        now = res.settings.model_dump(mode="json")
        diffs = tuple(
            f"{k}: backup {v!r}, camera {now.get(k)!r}"
            for k, v in payload["settings"].items()
            if now.get(k) != v
        )
        return RestoreReport(written=len(res.results), failed=failed, differences=diffs)

    def save_user_set(self, slot: str = "UserSet1", make_default: bool = True) -> dict[str, Any]:
        """The simulator keeps the saved settings in memory (for tests)."""
        self.user_sets = {**getattr(self, "user_sets", {}), slot: self.get_settings()}
        return {
            "slot": slot,
            "default": slot if make_default else None,
            "stored_features": None,
            "notes": [],
        }

    def load_user_set(self, slot: str = "Default") -> dict[str, Any]:
        """Default = the simulator's start-up settings; UserSetN = what was saved there."""
        if slot == "Default":
            self._settings = self._factory
        elif slot in getattr(self, "user_sets", {}):
            self._settings = self.user_sets[slot]
        else:
            raise ValueError(f"camera has no user set {slot!r}")
        return {"slot": slot, "notes": []}

    def close(self) -> None:
        """Stop and close."""
        self._streaming = False
        self._closed = True


class SimulatedCameraDriver(Camera2DDriver):
    """Driver exposing the single simulated camera ``SIM-0001``."""

    name: ClassVar[str] = "simulated"

    def __init__(
        self,
        light: SimulatedLight | None = None,
        *,
        seed: int = 0,
        ambient: float = 0.2,
        defocus_sigma: float = 0.0,
        black_level: float = 0.0,
        hot_spot_pct: float = 0.0,
        clock: Callable[[], datetime] = _utc_now,
        width: int = SENSOR_WIDTH,
        height: int = SENSOR_HEIGHT,
        sensor: SensorModel | None = None,
    ) -> None:
        if width < 16 or height < 16 or width % 16 or height % 2:
            raise ValueError("sensor width must be a multiple of 16 and height even, both >= 16")
        self.light = light
        self.seed = seed
        self.ambient = ambient
        self.defocus_sigma = defocus_sigma
        self.black_level = black_level
        self.hot_spot_pct = hot_spot_pct
        self.clock = clock
        self.width, self.height = width, height
        self.sensor = sensor

    def discover(self) -> list[CameraInfo]:
        """Return the simulated camera."""
        return [
            CameraInfo(
                vendor="camera_lab",
                model="Simulated2D",
                serial=SIM_SERIAL,
                firmware="sim-1",
                interface="simulated",
                ip=None,
                driver=self.name,
            )
        ]

    def open(self, camera_id: str) -> SimulatedCamera:
        """Open ``SIM-0001``."""
        if camera_id != SIM_SERIAL:
            raise CameraNotFoundError(f"simulated driver has no camera {camera_id!r}")
        return SimulatedCamera(
            self.discover()[0],
            light=self.light,
            seed=self.seed,
            ambient=self.ambient,
            defocus_sigma=self.defocus_sigma,
            black_level=self.black_level,
            hot_spot_pct=self.hot_spot_pct,
            clock=self.clock,
            width=self.width,
            height=self.height,
            sensor=self.sensor,
        )
