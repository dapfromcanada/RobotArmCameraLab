"""TwoDService: the 2D library API. CLI, MCP and GUI are thin layers over it.

It composes the camera drivers with core: safety limits, camera locks, the
light service, run records, presets and the calibration guard. Drivers know
nothing about any of these.

Settings flow (:meth:`TwoDService.apply`), the only way settings reach a camera:

1. the camera must be open in this process (``CameraNotOpenError``);
2. an active tuning run consumes one step first (``StepBudgetExhausted``);
3. every field is checked against its **effective range** (camera range ∩
   safety limit) and the camera's options: out of range → *rejected* (or
   *clamped* with ``mode="clamp"``, used only by the classic tuner); off the
   increment → *clamped* to the nearest valid value; unsupported → *rejected*;
4. the settings-write rate limit (``max_writes_per_s``) is applied;
5. the stream is stopped if a ``requires_stream_stop`` setting changes;
6. the driver applies the accepted values and reads them back;
7. read-back values outside the effective range and calibration-affecting
   changes produce warnings;
8. the step is logged (structlog to stderr, and the active run's ``steps.jsonl``).

:meth:`TwoDService.grab` is the public, **non-recording** grab (GUI live view,
calibration_lab). :meth:`TwoDService.capture` is built on it, then scores and
records.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import numpy as np
import numpy.typing as npt
import tifffile
from pydantic import ValidationError

from camera_lab import __version__
from camera_lab.core.config import ConfigError
from camera_lab.core.lights import (
    LightService,
    LightSetResult,
    SimulatedLight,
    create_light_controller,
    load_lights_config,
)
from camera_lab.core.locks import CameraBusyError as LockBusyError
from camera_lab.core.locks import CameraLock, LockDirError
from camera_lab.core.logging import get_logger
from camera_lab.core.paths import UnsafePathError, validate_serial
from camera_lab.core.presets import (
    Preset,
    PresetExistsError,
    PresetNotFoundError,
    PresetRef,
    PresetStore,
)
from camera_lab.core.records import (
    CaptureLimitError,
    RecordError,
    RunNotFoundError,
    RunRecord,
    RunRecorder,
    RunSummary,
    SoftwareInfo,
    list_runs,
    load_run,
    read_capture,
)
from camera_lab.core.safety import (
    BudgetError,
    Clamped,
    EffectiveRange,
    LoadedSafetyLimits,
    NotAdjustable,
    RateLimiter,
    Rejected,
    ResolvedCameraLimits,
    SafetyConfigError,
    check,
    check_choice,
    effective_range,
    load_safety_limits,
)
from camera_lab.two_d.backups import BackupError, BackupStore, CameraBackup, RestoreReport
from camera_lab.two_d.calib_guard import calibration_warnings
from camera_lab.two_d.camera_config import CamerasConfig, load_cameras_config
from camera_lab.two_d.charts.library import tag_owner
from camera_lab.two_d.charts.models import ChartSpec
from camera_lab.two_d.charts.scores import chart_records, chart_records_array
from camera_lab.two_d.drivers import driver_for_vendor, load_driver_class
from camera_lab.two_d.drivers.base import Camera2D, Camera2DDriver
from camera_lab.two_d.drivers.simulated import SimulatedCameraDriver
from camera_lab.two_d.errors import (
    AlreadyExistsError,
    CameraBusyError,
    CameraNotFoundError,
    CameraNotOpenError,
    ConfigurationError,
    DeviceError,
    InvalidRequestError,
    LimitRejectedError,
    NotFoundError,
    SdkUnavailableError,
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
    ScoreReport,
    SettingSpec,
)
from camera_lab.two_d.processing import make_preview
from camera_lab.two_d.profiles import CameraProfile, load_profile
from camera_lab.two_d.scoring import (
    ScoringConfig,
    ScoringParams,
    load_scoring_config,
    score_array,
    score_frame,
)
from camera_lab.two_d.strategies import STRATEGIES
from camera_lab.two_d.tuning import TuneResult, TuningRun

_log = get_logger(__name__)

Actor = str
REAL_DRIVERS = ("arena", "ebus")
_LIMITED = ("exposure_us", "gain_db", "frame_rate_hz")
MAX_GRAB_TIMEOUT_MS = 60_000


# ===================================================================== views
@dataclass(frozen=True)
class SettingsView:
    """Current settings + effective ranges/options of an open camera."""

    camera_id: str
    settings: CameraSettings
    ranges: dict[str, EffectiveRange | NotAdjustable]
    options: dict[str, tuple[str, ...]]
    affects_calibration: tuple[str, ...]
    requires_stream_stop: tuple[str, ...]
    #: Profile working ranges (guidance for tuning, never a limit), by setting name.
    working: dict[str, dict[str, float | None]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """JSON-able representation."""
        ranges: dict[str, Any] = {}
        for name, r in self.ranges.items():
            if isinstance(r, NotAdjustable):
                ranges[name] = {"adjustable": False, "reason": r.reason}
            else:
                ranges[name] = {
                    "adjustable": True,
                    "min": r.min.value,
                    "min_source": r.min.source,
                    "max": r.max.value,
                    "max_source": r.max.source,
                    "increment": r.increment,
                }
        return {
            "camera_id": self.camera_id,
            "settings": self.settings.model_dump(mode="json"),
            "ranges": ranges,
            "options": {k: list(v) for k, v in self.options.items()},
            "affects_calibration": list(self.affects_calibration),
            "requires_stream_stop": list(self.requires_stream_stop),
            "working_range": self.working,
        }


@dataclass(frozen=True)
class CaptureResult:
    """One recorded capture."""

    capture_id: str
    run_id: str
    frame_id: int
    settings: CameraSettings
    scores: ScoreReport
    preview_path: Path | None
    #: Test charts seen (Phase 10), as recorded in the capture sidecar.
    charts: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """JSON-able summary (scores without the histogram)."""
        return {
            "capture_id": self.capture_id,
            "run_id": self.run_id,
            "frame_id": self.frame_id,
            "settings": self.settings.model_dump(mode="json"),
            "composite": self.scores.composite,
            "metrics": {m.name: m.value for m in self.scores.metrics},
            "sub_scores": self.scores.sub_scores,
            "preview_path": str(self.preview_path) if self.preview_path else None,
            "charts": [
                {k: c.get(k) for k in ("chart_id", "version", "type", "tag_ids", "px_per_mm")}
                for c in self.charts
            ],
        }


@dataclass(frozen=True)
class FinishResult:
    """Outcome of :meth:`TwoDService.finish_run`."""

    record: RunRecord
    preset_path: Path | None


@dataclass(frozen=True)
class PresetApplyResult:
    """Outcome of :meth:`TwoDService.apply_preset`."""

    preset: Preset
    apply: ApplyResult
    lights: LightSetResult | None


@dataclass
class _OpenCamera:
    camera: Camera2D
    lock: CameraLock
    driver: str
    limits: ResolvedCameraLimits
    rate: RateLimiter
    last_frame: Frame | None = None
    mode: str | None = None  # operating mode entered in this session (None = not set)
    backed_up: bool = False  # a settings backup exists for this open session


RecorderFactory = Callable[..., RunRecorder]


# =================================================================== service
class TwoDService:
    """The 2D library API (thread-compatible: one front end per process)."""

    def __init__(
        self,
        limits: LoadedSafetyLimits,
        light_service: LightService | None = None,
        recorder_factory: RecorderFactory = RunRecorder.create,
        include_sim: bool = False,
        *,
        drivers: Mapping[str, Camera2DDriver] | None = None,
        scoring: ScoringConfig | None = None,
        cameras: CamerasConfig | None = None,
        presets: PresetStore | None = None,
        runs_root: Path | None = None,
        calibration_root: Path | None = None,
        profiles_root: Path | None = None,
        charts_root: Path | None = None,
        backups_root: Path | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.limits = limits
        self.light_service = light_service
        self._recorder_factory = recorder_factory
        self.include_sim = include_sim
        self._drivers: dict[str, Camera2DDriver] = dict(drivers or {})
        self._explicit_drivers = drivers is not None
        if include_sim and "simulated" not in self._drivers:
            light = light_service.controller if light_service is not None else None
            self._drivers["simulated"] = SimulatedCameraDriver(
                light if isinstance(light, SimulatedLight) else None
            )
        self.scoring = scoring or ScoringConfig()
        self.cameras = cameras or CamerasConfig()
        self.presets = presets or PresetStore()
        self._runs_root = runs_root
        self._calib_root = calibration_root
        self._profiles_root = profiles_root
        self._charts_root = charts_root
        self._backups = BackupStore(backups_root)
        self._profiles: dict[str, CameraProfile] = {}
        self._clock = clock
        self._sleep = sleep
        self._known: dict[str, tuple[str, CameraInfo]] = {}
        self._open: dict[str, _OpenCamera] = {}
        self._active_runs: dict[str, TuningRun] = {}

    # --------------------------------------------------------------- basics
    def resolve(self, camera_id: str) -> str:
        """Resolve an alias to a serial and validate it."""
        serial = self.cameras.resolve(camera_id)
        try:
            return validate_serial(serial)
        except UnsafePathError as exc:
            raise InvalidRequestError(str(exc)) from None

    def scoring_params(self, camera_id: str | None = None, tag: str | None = None) -> ScoringParams:
        """Scoring params: default ← camera ← current mode (profile) ← preset tag."""
        serial = self.cameras.resolve(camera_id) if camera_id else None
        params = self.scoring.resolve(serial)
        oc = self._open.get(serial) if serial else None
        if oc is not None and oc.mode is None and serial is not None:
            oc.mode = self._infer_mode(serial)
        if oc is not None and oc.mode is not None and serial is not None:
            _, mode = self.get_profile(serial).mode(oc.mode)
            params = params.model_copy(update=mode.scoring.model_dump(exclude_none=True))
        tag_ovr = self.scoring.tags.get(tag or "")
        if tag_ovr is not None:
            params = params.model_copy(update=tag_ovr.model_dump(exclude_none=True))
        return ScoringParams.model_validate(params.model_dump())

    def _load_real_drivers(self) -> None:
        if self._explicit_drivers:
            return
        for name in REAL_DRIVERS:
            if name in self._drivers:
                continue
            try:
                self._drivers[name] = load_driver_class(name)()
            except SdkUnavailableError as exc:
                _log.warning("driver_unavailable", driver=name, error=str(exc))

    # ------------------------------------------------------------ discovery
    def discover(self) -> list[CameraInfo]:
        """List cameras from every available driver (plus SIM-0001 with ``include_sim``)."""
        self._load_real_drivers()
        found: list[CameraInfo] = []
        for name, drv in self._drivers.items():
            try:
                infos = drv.discover()
            except Exception as exc:  # one broken driver must not hide the others
                _log.warning("discover_failed", driver=name, error=str(exc))
                continue
            for info in infos:
                if driver_for_vendor(info.vendor) != name:
                    _log.debug("discover_skip_vendor", driver=name, vendor=info.vendor)
                    continue
                self._known[info.serial] = (name, info)
                found.append(info)
        return sorted(found, key=lambda i: i.serial)

    # ----------------------------------------------------------- open/close
    def is_open(self, camera_id: str) -> bool:
        """True if the camera is open in this service."""
        return self.cameras.resolve(camera_id) in self._open

    def open(self, camera_id: str, owner: str = "cli") -> CameraInfo:
        """Lock and open a camera (idempotent within this service).

        Drivers disable all camera auto functions on open.

        Raises:
            CameraNotFoundError, CameraBusyError (exit 3), SdkUnavailableError (exit 5).
        """
        serial = self.resolve(camera_id)
        if serial in self._open:
            return self._open[serial].camera.info
        if serial not in self._known:
            self.discover()
        if serial not in self._known:
            raise CameraNotFoundError(f"camera {serial} not found")
        driver_name, _ = self._known[serial]
        lock = CameraLock(serial, owner)
        try:
            lock.acquire()
        except LockBusyError as exc:
            raise CameraBusyError(str(exc), exc.holder) from None
        except LockDirError as exc:
            raise ConfigurationError(str(exc)) from None
        try:
            camera = self._drivers[driver_name].open(serial)
        except BaseException:
            lock.release()
            raise
        lim = self.limits.camera(serial)
        oc = _OpenCamera(
            camera, lock, driver_name, lim, RateLimiter(lim.max_writes_per_s, clock=self._clock)
        )
        self._open[serial] = oc
        if camera.open_backup is not None:
            changes = camera.open_backup.pop("open_changes", None)
            reason = "on open, before camera_lab configured the camera"
            if changes:
                reason += ": " + "; ".join(str(c) for c in changes)
            try:
                self._save_backup(serial, oc, camera.open_backup, reason, settings={})
            except BaseException:
                self.close(serial)
                raise
            camera.open_backup = None
        _log.info("camera_opened", camera_id=serial, driver=driver_name, owner=owner)
        return camera.info

    # ------------------------------------------------------------- backups
    def _save_backup(
        self,
        serial: str,
        oc: _OpenCamera,
        payload: dict[str, Any],
        reason: str,
        settings: dict[str, Any] | None = None,
    ) -> tuple[CameraBackup, bool]:
        info = oc.camera.info
        try:
            backup, created = self._backups.save(
                serial=serial,
                driver=oc.driver,
                payload=payload,
                reason=reason,
                settings=settings,
                model=info.model,
                vendor=info.vendor,
                firmware=info.firmware or "",
            )
        except (OSError, BackupError, ValueError) as exc:
            raise DeviceError(
                f"settings backup could not be saved ({exc}); camera not changed"
            ) from None
        oc.backed_up = True
        _log.info(
            "camera_backup",
            camera_id=serial,
            backup_id=backup.backup_id,
            created=created,
            reason=reason,
        )
        return backup, created

    def _ensure_backup(self, serial: str, oc: _OpenCamera, reason: str) -> None:
        """Back up the camera once per open session, before the first change."""
        if oc.backed_up:
            return
        self._take_backup(serial, oc, reason)

    def _take_backup(self, serial: str, oc: _OpenCamera, reason: str) -> tuple[CameraBackup, bool]:
        try:
            payload = oc.camera.backup_settings()
            settings = oc.camera.get_settings().model_dump(mode="json")
        except NotImplementedError:
            raise ConfigurationError(
                f"camera {serial}: its driver cannot back up settings, "
                "so camera_lab will not change it"
            ) from None
        except Exception as exc:
            raise DeviceError(f"settings backup failed ({exc}); camera not changed") from None
        return self._save_backup(serial, oc, payload, reason, settings)

    def backup(self, camera_id: str, reason: str = "manual backup") -> tuple[CameraBackup, bool]:
        """Back up an open camera now; returns (backup, created). Identical → reused."""
        serial, oc = self._require_open(camera_id)
        return self._take_backup(serial, oc, reason[:300])

    def list_backups(self, camera_id: str) -> list[CameraBackup]:
        """Backups of a camera, oldest first (the camera need not be open)."""
        return self._backups.list(self.resolve(camera_id))

    def restore_backup(
        self, camera_id: str, backup_id: str, actor: Actor = "human"
    ) -> tuple[RestoreReport, str]:
        """Write a backup back; returns (report, id of the backup taken just before).

        Protected features (safety_limits.yaml) are never written (limits policy:
        camera values are limited only by the camera's own range).
        """
        serial, oc = self._require_open(camera_id)
        try:
            backup = self._backups.load(serial, backup_id)
        except BackupError as exc:
            raise NotFoundError(str(exc)) from None
        if backup.driver != oc.driver:
            raise InvalidRequestError(
                f"backup {backup_id} was made with the {backup.driver} driver, "
                f"camera uses {oc.driver}"
            )
        cam = oc.camera
        before, _ = self._take_backup(serial, oc, f"before restoring backup {backup_id}")
        if cam.is_streaming:
            cam.stop_stream()
        report = cam.restore_settings(backup.payload, oc.limits.is_protected)
        oc.mode = None
        _log.info(
            "camera_restored",
            camera_id=serial,
            backup_id=backup_id,
            actor=actor,
            written=report.written,
            failed=len(report.failed),
            differences=len(report.differences),
        )
        return report, before.backup_id

    def measure_charts(self, camera_id: str, timeout_ms: int = 5000) -> list[dict[str, Any]]:
        """Grab one frame (not recorded) and return the library charts seen, with scores."""
        library = self._chart_library()
        frame = self.grab(camera_id, timeout_ms)
        return chart_records(frame, library) if library else []

    def save_user_set(
        self,
        camera_id: str,
        slot: str = "UserSet1",
        *,
        confirm: bool,
        make_default: bool = True,
        actor: Actor = "human",
    ) -> dict[str, Any]:
        """Save the current settings to camera memory. Needs the human's confirmation.

        ``UserSetSave`` / ``UserSetDefault`` are protected features: this explicit,
        confirmed call is the only way camera_lab writes them. The camera is
        backed up first.
        """
        if confirm is not True:
            raise InvalidRequestError(
                "saving a user set writes camera memory: confirm=True is required"
            )
        serial, oc = self._require_open(camera_id)
        backup, _ = self._take_backup(serial, oc, f"before saving {slot}")
        try:
            out = oc.camera.save_user_set(slot, make_default)
        except NotImplementedError:
            raise ConfigurationError(f"camera {serial}: its driver cannot save user sets") from None
        except ValueError as exc:
            raise InvalidRequestError(str(exc)) from None
        _log.info(
            "user_set_saved", camera_id=serial, slot=slot, default=out.get("default"), actor=actor
        )
        return {**out, "backup_before": backup.backup_id}

    def user_set_info(self, camera_id: str) -> dict[str, Any]:
        """What the camera's user sets offer (read only)."""
        _, oc = self._require_open(camera_id)
        return oc.camera.user_set_info()

    def mode_mismatches(self, camera_id: str, mode: str | None = None) -> list[str]:
        """The settings of profile ``mode`` the camera does not have now (empty = in the mode).
        Values within the camera's rounding (1 %) count as equal."""
        serial, _ = self._require_open(camera_id)
        _, prof = self.get_profile(serial).mode(mode)
        now = self.get_settings(serial).settings
        want = prof.settings

        def near(a: float, b: float) -> bool:  # cameras round to their own increments
            return abs(a - b) <= max(1e-3, 0.01 * abs(b))

        out: list[str] = []
        for name in ("pixel_format", "gain_db", "exposure_us", "frame_rate_hz", "trigger_mode"):
            w, have = getattr(want, name), getattr(now, name)
            close = isinstance(w, float) and have is not None and near(float(have), w)
            if w is not None and have != w and not close:
                out.append(f"{name}: wanted {w}, camera has {have}")
        for ch, wv in (want.white_balance or {}).items():
            got = (now.white_balance or {}).get(ch)
            if got is None or not near(got, wv):
                out.append(f"white_balance.{ch}: wanted {wv}, camera has {got}")
        for key, wa in (want.advanced or {}).items():
            cur = now.advanced.get(key)
            same = (
                near(float(cur), float(wa))
                if isinstance(wa, int | float) and isinstance(cur, int | float)
                else cur == wa
            )
            if not same:
                out.append(f"{key}: wanted {wa}, camera has {cur}")
        return out

    def _infer_mode(self, serial: str) -> str | None:
        """The profile mode whose settings the camera has now (the default mode first): a new
        process (each CLI call) did not enter it itself but must still use its targets."""
        prof = self.get_profile(serial)
        order = [prof.default_mode, *(m for m in prof.modes if m != prof.default_mode)]
        for name in order:
            if not prof.modes[name].settings.changed_fields():
                continue  # a mode that sets nothing cannot be recognised
            try:
                if not self.mode_mismatches(serial, name):
                    return name
            except Exception:  # pragma: no cover - a camera that cannot report settings
                return None
        return None

    def reset_camera(
        self, camera_id: str, mode: str | None = None, actor: Actor = "human"
    ) -> dict[str, Any]:
        """Start from a known state: factory Default user set, then the profile mode (default:
        the design mode) with all its settings, then every one read back.

        Never trust the boot state (Derek 2026-10-23: a stray 28.6 dB in a user set skewed a
        day of measurements). Live only: nothing is written to the camera's flash. The camera
        is backed up first. ``mismatches`` lists the mode settings that did not take.
        """
        serial, oc = self._require_open(camera_id)
        backup, _ = self._take_backup(serial, oc, "before reset to factory Default")
        try:
            loaded = oc.camera.load_user_set("Default")
        except NotImplementedError:
            raise ConfigurationError(f"camera {serial}: its driver cannot load user sets") from None
        except ValueError as exc:
            raise InvalidRequestError(str(exc)) from None
        oc.mode = None
        result = self.set_mode(serial, mode, actor)
        mode_name, _ = self.get_profile(serial).mode(mode)
        now = self.get_settings(serial).settings
        mismatches = self.mode_mismatches(serial, mode_name)
        _log.info(
            "camera_reset",
            camera_id=serial,
            mode=mode_name,
            mismatches=len(mismatches),
            actor=actor,
        )
        return {
            "camera_id": serial,
            "loaded": loaded.get("slot"),
            "mode": mode_name,
            "applied": result.model_dump(mode="json"),
            "settings": now.model_dump(mode="json"),
            "mismatches": mismatches,
            "ok": not mismatches,
            "backup_before": backup.backup_id,
        }

    def close(self, camera_id: str) -> None:
        """Stop streaming, close the camera and release its lock (idempotent)."""
        serial = self.cameras.resolve(camera_id)
        oc = self._open.pop(serial, None)
        self._active_runs.pop(serial, None)
        if oc is None:
            return
        try:
            oc.camera.close()
        finally:
            oc.lock.release()
        _log.info("camera_closed", camera_id=serial)

    def close_all(self) -> None:
        """Close every open camera."""
        for serial in list(self._open):
            self.close(serial)

    def shutdown(self) -> None:
        """Close all cameras and turn the lights off."""
        try:
            self.close_all()
        finally:
            if self.light_service is not None:
                self.light_service.all_off()

    def __enter__(self) -> TwoDService:
        return self

    def __exit__(self, *exc: object) -> None:
        self.shutdown()

    def _require_open(self, camera_id: str) -> tuple[str, _OpenCamera]:
        serial = self.cameras.resolve(camera_id)
        oc = self._open.get(serial)
        if oc is None:
            raise CameraNotOpenError(
                f"camera {serial} is not open in this process; open() it first"
            )
        return serial, oc

    # ------------------------------------------------------------- settings
    @staticmethod
    def _spec_ranges(
        lim: ResolvedCameraLimits, caps: Mapping[str, SettingSpec]
    ) -> dict[str, EffectiveRange | NotAdjustable]:
        ranges: dict[str, EffectiveRange | NotAdjustable] = {}
        for name, spec in caps.items():
            if spec.kind == "enum":
                continue
            if name in _LIMITED:
                ranges[name] = lim.range_for(name, spec.camera_range())  # type: ignore[arg-type]
            elif "." in name:  # advanced setting: explicit limit, else by unit
                ranges[name] = lim.advanced_range(name, spec.unit, spec.camera_range())
            else:
                ranges[name] = effective_range(name, spec.camera_range(), None, None)
        return ranges

    def get_settings(self, camera_id: str) -> SettingsView:
        """Current settings, camera ranges and the profile's working ranges (guidance)."""
        serial, oc = self._require_open(camera_id)
        caps = oc.camera.capabilities()
        options: dict[str, tuple[str, ...]] = {}
        for name, spec in caps.items():
            if spec.kind == "enum":
                opts = spec.options
                if name == "pixel_format" and oc.limits.allowed_pixel_formats is not None:
                    opts = tuple(o for o in opts if o in oc.limits.allowed_pixel_formats)
                options[name] = opts
        ranges = self._spec_ranges(oc.limits, caps)
        profile = self.get_profile(serial)
        working = {}
        for name in ranges:
            wr = profile.working(name)
            if wr.min is not None or wr.max is not None:
                working[name] = {"min": wr.min, "max": wr.max}
        return SettingsView(
            camera_id=serial,
            settings=oc.camera.get_settings(),
            ranges=ranges,
            working=working,
            options=options,
            affects_calibration=tuple(n for n, s in caps.items() if s.affects_calibration),
            requires_stream_stop=tuple(n for n, s in caps.items() if s.requires_stream_stop),
        )

    def apply(
        self,
        camera_id: str,
        patch: CameraSettingsPatch,
        actor: Actor = "human",
        *,
        mode: Literal["reject", "clamp"] = "reject",
    ) -> ApplyResult:
        """Validate ``patch`` against the safety limits and camera, apply, read back.

        ``mode="clamp"`` is for the classic tuner only; every other caller rejects.
        """
        serial, oc = self._require_open(camera_id)
        run = self._active_runs.get(serial)
        if run is not None:
            run.use_step()
        cam = oc.camera
        caps = cam.capabilities()
        ranges = self._spec_ranges(oc.limits, caps)
        results: dict[str, FieldResult] = {}
        accepted: dict[str, Any] = {}

        def number(field: str, spec_name: str, value: Any) -> float | None:
            rng = ranges.get(spec_name)
            if rng is None:
                results[field] = FieldResult(
                    field=field,
                    status="rejected",
                    requested=value,
                    reason=f"{field} is not supported by camera {serial} in its current mode",
                )
                return None
            res = check(value, rng, mode=mode)
            if isinstance(res, Rejected):
                results[field] = FieldResult(
                    field=field, status="rejected", requested=value, reason=res.reason
                )
                return None
            v = float(res.value)
            if caps[spec_name].kind == "int":
                v = float(round(v))
            if isinstance(res, Clamped):
                results[field] = FieldResult(
                    field=field, status="clamped", requested=value, value=v, reason=res.reason
                )
            else:
                results[field] = FieldResult(
                    field=field, status="applied", requested=value, value=v
                )
            return v

        for name in ("exposure_us", "gain_db", "gamma", "frame_rate_hz"):
            value = getattr(patch, name)
            if value is not None:
                v = number(name, name, value)
                if v is not None:
                    accepted[name] = v
        if patch.white_balance is not None:
            wb: dict[str, float] = {}
            for ch, value in patch.white_balance.items():
                v = number(f"white_balance.{ch}", f"wb_{ch}", value)
                if v is not None:
                    wb[ch] = v
            if wb:
                accepted["white_balance"] = wb
        if patch.roi is not None:
            parts: dict[str, int] = {}
            sub: dict[str, FieldResult] = {}
            for part in ("x", "y", "width", "height"):
                v = number(f"roi.{part}", f"roi_{part}", getattr(patch.roi, part))
                sub[part] = results.pop(f"roi.{part}")
                if v is not None:
                    parts[part] = int(v)
            bad = [r.reason for r in sub.values() if r.status == "rejected" and r.reason]
            clamped = [r.reason for r in sub.values() if r.status == "clamped" and r.reason]
            if bad:
                results["roi"] = FieldResult(
                    field="roi",
                    status="rejected",
                    requested=patch.roi.model_dump(),
                    reason="; ".join(bad),
                )
            else:
                accepted["roi"] = ROI(**parts)
                results["roi"] = FieldResult(
                    field="roi",
                    status="clamped" if clamped else "applied",
                    requested=patch.roi.model_dump(),
                    value=parts,
                    reason="; ".join(clamped) or None,
                )
        if patch.pixel_format is not None:
            if "pixel_format" in caps:
                res = oc.limits.check_pixel_format(patch.pixel_format, caps["pixel_format"].options)
            else:
                res = Rejected(patch.pixel_format, "pixel_format is not supported by camera")
            self._choice_result(results, accepted, "pixel_format", patch.pixel_format, res)
        if patch.trigger_mode is not None:
            if "trigger_mode" in caps:
                res = check_choice(
                    "trigger_mode", patch.trigger_mode, caps["trigger_mode"].options, None, ""
                )
            else:
                res = Rejected(patch.trigger_mode, "trigger_mode is not supported by camera")
            self._choice_result(results, accepted, "trigger_mode", patch.trigger_mode, res)

        if patch.advanced is not None:
            adv_ok: dict[str, Any] = {}
            for key, value in patch.advanced.items():
                field = f"advanced.{key}"
                spec = caps.get(key)
                if spec is None or "." not in key:
                    results[field] = FieldResult(
                        field=field,
                        status="rejected",
                        requested=value,
                        reason=f"advanced setting {key!r} is not supported by camera {serial}",
                    )
                    continue
                if spec.kind == "enum":
                    res = check_choice(key, value, spec.options, None, "")
                    self._choice_result(results, adv_ok, field, value, res)
                    if field in adv_ok:
                        adv_ok[key] = adv_ok.pop(field)
                else:
                    v = number(field, key, value)
                    if v is not None:
                        adv_ok[key] = v
            if adv_ok:
                accepted["advanced"] = adv_ok

        before = cam.get_settings()
        warnings: list[str] = []
        if accepted:
            oc.rate.acquire(self._sleep)
            vpatch = CameraSettingsPatch(**accepted)
            self._ensure_backup(serial, oc, "before the first settings change in this session")
            if cam.is_streaming and self._needs_stream_stop(vpatch, before, caps):
                cam.stop_stream()
                _log.info("stream_stopped_for_apply", camera_id=serial)
            dres = cam.apply(vpatch)
            for r in dres.results:
                key = "roi" if r.field.startswith("roi") else r.field
                prev = results.get(key)
                if r.status == "applied" and prev is not None and prev.status == "clamped":
                    results[key] = prev.model_copy(update={"value": r.value})
                else:
                    results[key] = r
            after = dres.settings
            warnings.extend(dres.warnings)
        else:
            after = before
        # Side effects: a setting that was not asked for but changed with the others (e.g. the
        # camera shortens the exposure to fit a higher frame rate) is reported, not silent.
        asked = set(patch.changed_fields())
        for name, unit in (("exposure_us", "µs"), ("frame_rate_hz", "Hz"), ("gain_db", "dB")):
            b, a2 = getattr(before, name), getattr(after, name)
            moved = (
                b is not None
                and a2 is not None
                and abs(float(a2) - float(b)) > max(1e-6, 1e-3 * abs(float(b)))
            )
            if name not in asked and moved:
                warnings.append(f"{name} changed from {b:g} to {a2:g} {unit} as a side effect")
        # Re-read the ranges: a format/ROI/frame-rate change moves the camera's limits.
        after_ranges = self._spec_ranges(oc.limits, cam.capabilities()) if accepted else ranges
        warnings.extend(self._readback_warnings(after, after_ranges))
        roi_specs = [caps[n] for n in ("roi_x", "roi_y", "roi_width", "roi_height") if n in caps]
        if after.roi != before.roi and any(spec.affects_calibration for spec in roi_specs):
            warnings.extend(calibration_warnings(serial, ["roi"], self._calib_root))
        ordered = tuple(results.values())
        result = ApplyResult(results=ordered, settings=after, warnings=tuple(warnings))
        rejections = [r.reason or r.field for r in ordered if r.status == "rejected"]
        for w in warnings:
            _log.warning("apply_warning", camera_id=serial, warning=w)
        _log.info(
            "settings_applied",
            camera_id=serial,
            actor=actor,
            mode=mode,
            fields={r.field: r.status for r in ordered},
        )
        if run is not None:
            run.log(
                actor,
                "set_camera_settings",
                patch.model_dump(mode="json", exclude_none=True),
                {
                    "ok": result.ok,
                    "results": [r.model_dump(mode="json") for r in ordered],
                    "warnings": list(warnings),
                },
                rejections,
            )
        return result

    @staticmethod
    def _choice_result(
        results: dict[str, FieldResult],
        accepted: dict[str, Any],
        field: str,
        value: Any,
        res: Any,
    ) -> None:
        if isinstance(res, Rejected):
            results[field] = FieldResult(
                field=field, status="rejected", requested=value, reason=res.reason
            )
        else:
            results[field] = FieldResult(
                field=field, status="applied", requested=value, value=value
            )
            accepted[field] = value

    @staticmethod
    def _needs_stream_stop(
        patch: CameraSettingsPatch, cur: CameraSettings, caps: Mapping[str, SettingSpec]
    ) -> bool:
        def stops(name: str) -> bool:
            return name in caps and caps[name].requires_stream_stop

        fmt_changed = patch.pixel_format is not None and patch.pixel_format != cur.pixel_format
        if fmt_changed and stops("pixel_format"):
            return True
        if patch.roi is not None:
            for part in ("x", "y", "width", "height"):
                if getattr(patch.roi, part) != getattr(cur.roi, part) and stops(f"roi_{part}"):
                    return True
        for name in ("exposure_us", "gain_db", "gamma", "frame_rate_hz", "trigger_mode"):
            new = getattr(patch, name)
            if new is not None and new != getattr(cur, name) and stops(name):
                return True
        for key, value in (patch.advanced or {}).items():
            if value != cur.advanced.get(key) and stops(key):
                return True
        return False

    @staticmethod
    def _readback_warnings(
        s: CameraSettings, ranges: Mapping[str, EffectiveRange | NotAdjustable]
    ) -> list[str]:
        out = []
        for name in _LIMITED:
            rng = ranges.get(name)
            value = getattr(s, name)
            if value is None or not isinstance(rng, EffectiveRange):
                continue
            tol = rng.increment or 0.0
            if not rng.min.value - tol <= value <= rng.max.value + tol:
                out.append(
                    f"read-back {name} {value} is outside the effective range "
                    f"[{rng.min.value}, {rng.max.value}]"
                )
        return out

    # --------------------------------------------------------- grab/capture
    def grab(self, camera_id: str, timeout_ms: int = 2000) -> Frame:
        """Return one frame **without recording anything** (public API).

        The camera must already be open in this process. The stream is started
        if needed and keeps running until :meth:`close` or a stream-stopping
        :meth:`apply`. Writes no files, no ``steps.jsonl`` line and computes no
        scores; uses no tuning-run step.

        Raises:
            CameraNotOpenError: the camera is not open in this process.
            CaptureTimeoutError / IncompleteFrameError: no (complete) frame (exit 6).
        """
        serial, oc = self._require_open(camera_id)
        if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int):
            raise InvalidRequestError("timeout_ms must be an integer")
        if not 1 <= timeout_ms <= MAX_GRAB_TIMEOUT_MS:
            raise InvalidRequestError(f"timeout_ms must be within 1..{MAX_GRAB_TIMEOUT_MS}")
        if not oc.camera.is_streaming:
            oc.camera.start_stream()
        frame = oc.camera.grab(timeout_ms)
        oc.last_frame = frame
        _log.debug("frame_grabbed", camera_id=serial, frame_id=frame.frame_id)
        return frame

    def last_frame(self, camera_id: str) -> Frame | None:
        """The most recent frame grabbed on this camera (in memory only)."""
        _, oc = self._require_open(camera_id)
        return oc.last_frame

    def _light_levels(self) -> dict[str, float]:
        return self.light_service.get_levels() if self.light_service is not None else {}

    def _software(self, serial: str, oc: _OpenCamera) -> SoftwareInfo:
        drv = self._drivers.get(oc.driver)
        return SoftwareInfo(
            camera_lab=__version__,
            sdk_name=oc.driver,
            sdk_version=str(getattr(drv, "sdk_version", "")) or None,
            firmware=oc.camera.info.firmware,
        )

    def _new_recorder(
        self,
        serial: str,
        oc: _OpenCamera,
        method: Literal["agent", "classic", "manual", "setup"],
        operator: Literal["human", "agent"],
        goal: str,
        max_steps: int,
    ) -> RunRecorder:
        return self._recorder_factory(
            oc.camera.info,
            method,
            operator,
            goal,
            max_steps,
            software=self._software(serial, oc),
            limits_hash=self.limits.sha256,
            max_captures=self.limits.limits.tuning.max_captures_per_run,
            root=self._runs_root,
        )

    def capture(
        self,
        camera_id: str,
        count: int = 1,
        run_id: str | None = None,
        note: str = "",
        actor: Actor = "human",
    ) -> list[CaptureResult]:
        """Grab ``count`` frames, score and record them.

        Uses ``run_id`` if given, else the camera's active tuning run, else a new
        ``manual`` run that is finished straight away. One capture call is one
        step of an active run.
        """
        serial, oc = self._require_open(camera_id)
        cap_max = self.limits.limits.tuning.max_captures_per_run
        if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= cap_max:
            raise InvalidRequestError(f"count must be an integer within 1..{cap_max}")
        if len(note) > 500:
            raise InvalidRequestError("note must be at most 500 characters")
        active = self._active_runs.get(serial)
        run: TuningRun | None = None
        own = False
        if run_id is None and active is not None:
            run, recorder = active, active.recorder
        elif run_id is None:
            operator: Literal["human", "agent"] = "agent" if actor == "agent" else "human"
            recorder = self._new_recorder(serial, oc, "manual", operator, note, 0)
            own = True
        elif active is not None and active.run_id == run_id:
            run, recorder = active, active.recorder
        else:
            recorder = self._load_recorder(run_id)
            if recorder.record.camera.get("serial") != serial:
                raise InvalidRequestError(f"run {run_id} belongs to another camera")
            if recorder.record.finished_at is not None:
                raise InvalidRequestError(f"run {run_id} is already finished")
        if run is not None:
            run.use_step()
        params = self.scoring_params(serial)
        library = self._chart_library()
        results: list[CaptureResult] = []
        try:
            for _ in range(count):
                frame = self.grab(serial)
                report = score_frame(frame, params)
                charts = chart_records(frame, library) if library else []
                capture_id = recorder.add_capture(
                    frame.data,
                    pixel_format=frame.pixel_format,
                    bit_depth=frame.bit_depth,
                    settings=frame.settings,
                    lights=self._light_levels(),
                    scores=report,
                    frame_id=frame.frame_id,
                    preview=make_preview(frame),
                    note=note,
                    charts=charts,
                )
                preview = recorder.run_dir / "previews" / f"{capture_id[-4:]}.jpg"
                results.append(
                    CaptureResult(
                        capture_id,
                        recorder.run_id,
                        frame.frame_id,
                        frame.settings,
                        report,
                        preview if preview.exists() else None,
                        tuple(charts),
                    )
                )
        except CaptureLimitError as exc:
            raise LimitRejectedError(str(exc)) from None
        finally:
            recorder.log_step(
                actor,
                "capture",
                {"count": count, "note": note},
                {
                    "capture_ids": [r.capture_id for r in results],
                    "composite": [r.scores.composite for r in results],
                },
            )
            if own:
                best = max(results, key=lambda r: r.scores.composite, default=None)
                recorder.finish(
                    best_capture_id=best.capture_id if best else None, summary="manual capture"
                )
        _log.info("captured", camera_id=serial, run_id=recorder.run_id, count=len(results))
        return results

    def _chart_library(self) -> dict[int, ChartSpec]:
        """Tag id → chart; empty (detection off) if the library is missing or invalid."""
        try:
            return tag_owner(self._charts_root)
        except (ConfigError, OSError, ValueError) as exc:
            _log.warning("chart_library_unavailable", error=str(exc)[:300])
            return {}

    def _load_recorder(self, run_id: str) -> RunRecorder:
        try:
            return RunRecorder.load(
                run_id,
                root=self._runs_root,
                max_captures=self.limits.limits.tuning.max_captures_per_run,
            )
        except RunNotFoundError as exc:
            raise NotFoundError(str(exc)) from None
        except RecordError as exc:
            raise InvalidRequestError(str(exc)) from None

    # -------------------------------------------------------------- scoring
    def score_file(
        self, path: Path, pixel_format: str | None = None, bit_depth: int | None = None
    ) -> ScoreReport:
        """Score a TIFF file (pixel format from its camera_lab sidecar, else inferred)."""
        data, pixel_format, bit_depth, serial = self._read_tiff(path, pixel_format, bit_depth)
        try:
            return score_array(data, pixel_format, bit_depth, self.scoring_params(serial))
        except (TypeError, ValueError) as exc:
            raise InvalidRequestError(f"{path}: {exc}") from None

    def score_charts_file(
        self, path: Path, pixel_format: str | None = None, bit_depth: int | None = None
    ) -> list[dict[str, Any]]:
        """Detect and score the library test charts in a TIFF file (deterministic)."""
        data, pixel_format, bit_depth, _ = self._read_tiff(path, pixel_format, bit_depth)
        library = self._chart_library()
        if not library:
            return []
        try:
            return chart_records_array(data, pixel_format, bit_depth, library)
        except (TypeError, ValueError) as exc:
            raise InvalidRequestError(f"{path}: {exc}") from None

    def _read_tiff(
        self, path: Path, pixel_format: str | None, bit_depth: int | None
    ) -> tuple[npt.NDArray[Any], str, int, str | None]:
        """Read a TIFF; pixel format / bit depth from its camera_lab sidecar, else inferred."""
        p = Path(path)
        if p.suffix.lower() not in (".tif", ".tiff") or not p.is_file():
            raise InvalidRequestError(f"{p}: not a .tif/.tiff file")
        try:
            with tifffile.TiffFile(p) as tf:
                page = tf.pages.first
                data = np.asarray(page.asarray())
                desc = page.description or ""
        except (OSError, ValueError, tifffile.TiffFileError) as exc:
            raise InvalidRequestError(f"{p}: cannot read TIFF: {exc}") from None
        serial = None
        try:
            meta = json.loads(desc)
            if isinstance(meta, dict):
                pixel_format = pixel_format or meta.get("pixel_format")
                bit_depth = bit_depth or meta.get("bit_depth")
                cid = meta.get("capture_id")
                if isinstance(cid, str) and "_" in cid:
                    serial = cid.split("_")[1]
        except ValueError:
            pass
        if pixel_format is None:
            colour = data.ndim == 3
            pixel_format = {
                (np.dtype(np.uint8), False): "Mono8",
                (np.dtype(np.uint8), True): "RGB8",
                (np.dtype(np.uint16), False): "Mono16",
                (np.dtype(np.uint16), True): "RGB16",
            }.get((data.dtype, colour))
            if pixel_format is None:
                raise InvalidRequestError(f"{p}: unsupported image dtype {data.dtype}")
        if bit_depth is None:
            bit_depth = PIXEL_FORMAT_BITS.get(pixel_format)
        if bit_depth is None:
            raise InvalidRequestError(f"{p}: unknown bit depth for {pixel_format}")
        return data, pixel_format, bit_depth, serial

    def score_capture(self, capture_id: str) -> ScoreReport:
        """Recompute the scores of a recorded capture from disk."""
        try:
            data, side = read_capture(capture_id, root=self._runs_root)
        except (RunNotFoundError, ValueError) as exc:
            raise NotFoundError(str(exc)) from None
        serial = capture_id.split("_")[1]
        return score_array(data, side.pixel_format, side.bit_depth, self.scoring_params(serial))

    # ----------------------------------------------------------------- runs
    def start_run(
        self,
        camera_id: str,
        goal: str,
        max_steps: int,
        *,
        method: Literal["agent", "classic", "manual", "setup"] = "agent",
        operator: Literal["human", "agent"] = "agent",
    ) -> TuningRun:
        """Start a step-budgeted tuning run on an open camera."""
        serial, oc = self._require_open(camera_id)
        if serial in self._active_runs:
            raise AlreadyExistsError(
                f"camera {serial} already has an active run {self._active_runs[serial].run_id}"
            )
        if len(goal) > 500:
            raise InvalidRequestError("goal must be at most 500 characters")
        try:
            budget = self.limits.step_budget(max_steps)
        except BudgetError as exc:
            raise LimitRejectedError(str(exc)) from None
        recorder = self._new_recorder(serial, oc, method, operator, goal, max_steps)
        run = TuningRun(recorder, budget, serial, method)
        self._active_runs[serial] = run
        run.log(
            operator, "start_tuning_run", {"goal": goal, "max_steps": max_steps, "method": method}
        )
        _log.info("run_started", run_id=run.run_id, camera_id=serial, method=method)
        return run

    def active_run(self, camera_id: str) -> TuningRun | None:
        """The active tuning run of a camera, if any."""
        return self._active_runs.get(self.cameras.resolve(camera_id))

    def finish_run(
        self,
        run_id: str,
        best_capture_id: str | None,
        preset_name: str | None = None,
        summary: str = "",
        actor: Actor = "agent",
        *,
        overwrite: bool = False,
    ) -> FinishResult:
        """Finish an active run; optionally save its best capture as a preset."""
        run = next((r for r in self._active_runs.values() if r.run_id == run_id), None)
        if run is None:
            raise NotFoundError(f"no active run {run_id!r}")
        if len(summary) > 2000:
            raise InvalidRequestError("summary must be at most 2000 characters")
        preset_path = None
        if preset_name is not None:
            if best_capture_id is None:
                raise InvalidRequestError("a preset needs a best_capture_id")
            preset_path = self.save_preset(
                run.camera_id,
                preset_name,
                best_capture_id,
                overwrite=overwrite,
                created_by="agent" if actor == "agent" else "human",
            )
        run.log(
            actor,
            "finish_tuning_run",
            {"best_capture_id": best_capture_id, "preset_name": preset_name},
        )
        try:
            record = run.finish(
                best_capture_id=best_capture_id,
                summary=summary,
                preset_saved=str(preset_path) if preset_path else None,
            )
        except (RecordError, ValueError) as exc:
            raise InvalidRequestError(str(exc)) from None
        self._active_runs.pop(run.camera_id, None)
        return FinishResult(record, preset_path)

    def list_runs(self, camera: str | None = None, method: str | None = None) -> list[RunSummary]:
        """Run summaries, newest first."""
        serial = self.cameras.resolve(camera) if camera else None
        return list_runs(root=self._runs_root, camera=serial, method=method)

    def get_run(self, run_id: str) -> RunRecord:
        """The ``run.json`` of a run."""
        try:
            return load_run(run_id, root=self._runs_root)
        except RunNotFoundError as exc:
            raise NotFoundError(str(exc)) from None
        except RecordError as exc:
            raise InvalidRequestError(str(exc)) from None

    # -------------------------------------------------------------- presets
    def save_preset(
        self,
        camera_id: str,
        name: str,
        from_capture: str,
        *,
        overwrite: bool = False,
        tags: dict[str, Any] | None = None,
        created_by: Literal["human", "agent"] = "human",
    ) -> Path:
        """Save the settings + lights of a recorded capture as a named preset."""
        serial = self.resolve(camera_id)
        try:
            _, side = read_capture(from_capture, root=self._runs_root)
            record = load_run(from_capture.rsplit("/", 1)[0], root=self._runs_root)
        except (RunNotFoundError, ValueError) as exc:
            raise NotFoundError(str(exc)) from None
        cam = record.camera
        if cam.get("serial") != serial:
            raise InvalidRequestError(f"capture {from_capture} is not from camera {serial}")
        scores = {k: v for k, v in side.scores.items() if k != "histogram"}
        try:
            preset = self.presets.new(
                name=name,
                camera={k: cam.get(k) for k in ("vendor", "model", "serial", "firmware")},
                settings=side.settings,
                lights=side.lights,
                tags=tags or {},
                source_capture_id=from_capture,
                scores_at_save=scores,
                created_by=created_by,
            )
            return self.presets.save(preset, overwrite=overwrite)
        except PresetExistsError as exc:
            raise AlreadyExistsError(str(exc)) from None
        except (ValidationError, UnsafePathError) as exc:
            raise InvalidRequestError(f"invalid preset: {exc}") from None

    def get_preset(self, camera_id: str, name: str) -> Preset:
        """Load one preset."""
        serial = self.resolve(camera_id)
        try:
            return self.presets.load(serial, name)
        except PresetNotFoundError as exc:
            raise NotFoundError(str(exc)) from None
        except UnsafePathError as exc:
            raise InvalidRequestError(str(exc)) from None
        except ConfigError as exc:
            raise ConfigurationError(str(exc)) from None

    def list_presets(self, camera_id: str | None = None) -> list[PresetRef]:
        """List presets (all cameras or one)."""
        serial = self.resolve(camera_id) if camera_id else None
        return self.presets.list(serial)

    def apply_preset(self, camera_id: str, name: str, actor: Actor = "human") -> PresetApplyResult:
        """Apply a preset's settings (through :meth:`apply`) and light levels."""
        preset = self.get_preset(camera_id, name)
        patch = CameraSettingsPatch.model_validate(
            {k: v for k, v in preset.settings.items() if k in CameraSettingsPatch.model_fields}
        )
        result = self.apply(camera_id, patch, actor)
        lights = self.set_lights(preset.lights, actor) if preset.lights else None
        return PresetApplyResult(preset, result, lights)

    # --------------------------------------------------------------- lights
    def _require_lights(self) -> LightService:
        if self.light_service is None:
            raise ConfigurationError("no light controller configured")
        return self.light_service

    def list_lights(self) -> list[dict[str, Any]]:
        """Channels with current level, hardware max and safety max."""
        ls = self._require_lights()
        levels = ls.get_levels()
        return [
            {
                "id": c.id,
                "name": c.name,
                "colour": c.colour,
                "level_pct": levels.get(c.id, 0.0),
                "hardware_max_pct": c.max_pct,
                "max_pct": min(c.max_pct, ls.limits_for(c.id).max_pct),
                "max_step_pct": ls.limits_for(c.id).max_step_pct,
            }
            for c in ls.channels()
        ]

    def set_lights(self, levels: Mapping[str, Any], actor: Actor = "human") -> LightSetResult:
        """Set light levels through the light safety layer (one step per active run)."""
        ls = self._require_lights()
        runs = list(self._active_runs.values())
        for run in runs:
            run.use_step()
        result = ls.set_levels(levels)
        for run in runs:
            run.log(
                actor,
                "set_lights",
                {str(k): v for k, v in levels.items()},
                {"ok": result.ok, "levels": result.levels, "ramp_steps": result.ramp_steps},
                [r.reason or r.channel for r in result.results if r.status == "rejected"],
            )
        return result

    def lights_off(self, actor: Actor = "human") -> dict[str, float]:
        """Turn all lights off (always allowed; uses no step)."""
        ls = self._require_lights()
        ls.all_off()
        for run in self._active_runs.values():
            run.log(actor, "lights_off", {}, {"ok": True})
        return ls.get_levels()

    # ------------------------------------------------------- profiles/modes
    def get_profile(self, camera_id: str) -> CameraProfile:
        """The camera's profile (``config/camera_profiles/<serial>.yaml`` or generic)."""
        serial = self.resolve(camera_id)
        if serial not in self._profiles:
            try:
                self._profiles[serial] = load_profile(serial, self._profiles_root)
            except ConfigError as exc:
                raise ConfigurationError(str(exc)) from None
        return self._profiles[serial]

    def current_mode(self, camera_id: str) -> str | None:
        """The mode entered in this session, or the profile mode the camera's settings match
        (``None`` if neither)."""
        serial, oc = self._require_open(camera_id)
        if oc.mode is None:
            oc.mode = self._infer_mode(serial)
        return oc.mode

    def set_mode(
        self, camera_id: str, mode: str | None = None, actor: Actor = "human"
    ) -> ApplyResult:
        """Enter a profile mode: apply its settings through :meth:`apply`."""
        serial, oc = self._require_open(camera_id)
        try:
            name, prof = self.get_profile(serial).mode(mode)
        except KeyError as exc:
            raise NotFoundError(str(exc.args[0])) from None
        result = self.apply(serial, prof.settings, actor)
        if not result.ok:
            reasons = "; ".join(
                r.reason or r.field for r in result.results if r.status == "rejected"
            )
            raise LimitRejectedError(f"cannot enter mode {name!r} on {serial}: {reasons}")
        oc.mode = name
        _log.info("mode_set", camera_id=serial, mode=name, strategy=prof.strategy)
        return result

    def tune(
        self,
        camera_id: str,
        mode: str | None = None,
        strategy: str | None = None,
        *,
        max_steps: int = 15,
        target_mean: float | None = None,
        white_balance: bool = False,
        operator: Literal["human", "agent"] = "human",
        actor: Actor = "classic",
        options: Mapping[str, Any] | None = None,
    ) -> TuneResult:
        """Enter ``mode`` (default: the profile's) and run its (or ``strategy``'s) tuner.

        ``options`` override the mode's strategy options for this run only
        (e.g. ``{"wb_method": "exposure"}``).
        """
        serial, _ = self._require_open(camera_id)
        self.set_mode(serial, mode, actor="human" if operator == "human" else "agent")
        name, prof = self.get_profile(serial).mode(mode)
        if options:
            prof = prof.model_copy(update={"options": {**prof.options, **options}})
        strat_name = strategy or prof.strategy
        try:
            strat = STRATEGIES.get(strat_name)()
        except KeyError:
            raise InvalidRequestError(
                f"unknown strategy {strat_name!r}; known: {', '.join(STRATEGIES.names())}"
            ) from None
        result = strat.run(
            self,
            serial,
            prof,
            max_steps=max_steps,
            target_mean=target_mean,
            white_balance=white_balance,
            operator=operator,
            actor=actor,
        )
        result.mode, result.strategy = name, strat_name
        return result

    # --------------------------------------------------------------- limits
    def limits_view(self) -> dict[str, Any]:
        """The effective safety limits (read-only)."""
        return {
            "source": self.limits.source,
            "sha256": self.limits.sha256,
            "limits": self.limits.limits.model_dump(mode="json"),
        }


# =================================================================== factory
def build_service(
    *,
    sim: bool = False,
    limits_path: Path | None = None,
    lights_path: Path | None = None,
    scoring_path: Path | None = None,
    cameras_path: Path | None = None,
) -> TwoDService:
    """Build a service from the config files (what the CLI, MCP and GUI use).

    Raises:
        ConfigurationError: a config file is missing/invalid (safety limits fall
            back to the built-in sim defaults only with ``sim=True``).
    """
    try:
        limits = load_safety_limits(limits_path, sim=sim)
    except SafetyConfigError as exc:
        raise ConfigurationError(str(exc)) from None
    try:
        lights_cfg = load_lights_config(lights_path)
        controller = create_light_controller(lights_cfg)
    except (ConfigError, KeyError) as exc:
        if not sim:
            raise ConfigurationError(str(exc)) from None
        controller = SimulatedLight()
    try:
        scoring = load_scoring_config(scoring_path)
        cameras = load_cameras_config(cameras_path)
    except ConfigError as exc:
        raise ConfigurationError(str(exc)) from None
    return TwoDService(
        limits,
        LightService(controller, limits),
        include_sim=sim,
        scoring=scoring,
        cameras=cameras,
    )
