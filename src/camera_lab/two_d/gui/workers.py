"""Camera worker: all camera and service calls run on one ``QThread``.

The UI thread never calls the service or the camera SDK. It emits
``job_requested(name, kwargs)`` (queued), the worker runs the job and answers
with ``job_done(name, payload)`` or ``job_failed(name, code, message)``.

Live loop: a single-shot timer in the worker thread; each tick does
``service.grab()`` -> downscaled preview -> fast scores (mean, clipping,
histogram on the preview) -> ``frame_ready(QImage, dict)``. Queued jobs run
between ticks, and the preview rate is capped at :data:`MAX_LIVE_FPS`.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import numpy.typing as npt
from PySide6.QtCore import QObject, QTimer, Signal, Slot
from PySide6.QtGui import QImage

from camera_lab.core.logging import get_logger
from camera_lab.core.paths import safe_join, two_d_runs_root
from camera_lab.core.presets import PresetRef
from camera_lab.core.records import CaptureSidecar, RunSummary
from camera_lab.two_d.errors import CameraBusyError, InvalidRequestError, TwoDError
from camera_lab.two_d.gui.constants import (
    ACTOR,
    FPS_WINDOW_FRAMES,
    HISTOGRAM_BINS,
    LIVE_GRAB_TIMEOUT_MS,
    LIVE_MAX_CONSECUTIVE_ERRORS,
    LOCK_OWNER,
    MAX_LIVE_FPS,
    PREVIEW_LONG_EDGE_PX,
    TUNE_MAX_STEPS,
)
from camera_lab.two_d.models import ApplyResult, CameraInfo, Frame, ProcessorResult
from camera_lab.two_d.processing import (
    PROCESSORS,
    REC709,
    demosaic,
    make_preview,
    to_display_array,
)
from camera_lab.two_d.service import (
    CaptureResult,
    PresetApplyResult,
    SettingsView,
    TwoDService,
)
from camera_lab.two_d.setup import SetupSession, StepName, StepResult
from camera_lab.two_d.tuning import TuneResult

_log = get_logger(__name__)

MAX_PIXEL = 255
OVERLAY_RGBA = (231, 76, 60, 190)  # ACCENT_RED with alpha
PERCENT = 100.0
CHANNELS_RGBA = 4
CHANNELS_RGB = 3
HISTOGRAM_RANGE = (0.0, 1.0)
LIVE_MIN_INTERVAL_S = 1.0 / MAX_LIVE_FPS
MS_PER_S = 1000.0
MIN_FPS_SAMPLES = 2
CAPTURE_JSON_GLOB = "[0-9][0-9][0-9][0-9].json"
STATS_ROUND_DIGITS = 6
SUMMARY_ROUND_DIGITS = 4


# ============================================================== job payloads
@dataclass(frozen=True)
class OpenOutcome:
    """Result of the ``open`` job."""

    info: CameraInfo
    view: SettingsView
    modes: tuple[str, ...]
    mode: str | None
    default_mode: str | None


@dataclass(frozen=True)
class ApplyOutcome:
    """Result of ``apply`` / ``set_mode`` / ``tune`` style jobs that change settings."""

    result: ApplyResult | None
    view: SettingsView
    mode: str | None = None


@dataclass(frozen=True)
class CaptureOutcome:
    """Result of the ``capture`` job."""

    captures: list[CaptureResult]
    processors: list[ProcessorResult] = field(default_factory=list)


@dataclass(frozen=True)
class TuneOutcome:
    """Result of the ``tune`` job."""

    result: TuneResult
    view: SettingsView
    mode: str | None


@dataclass(frozen=True)
class LightOutcome:
    """Result of the ``lights_*`` jobs: per-channel results (if any) + channel states."""

    channels: list[dict[str, Any]]
    rejected: tuple[str, ...] = ()


@dataclass(frozen=True)
class PresetOutcome:
    """Result of ``preset_apply``."""

    applied: PresetApplyResult
    view: SettingsView
    channels: list[dict[str, Any]]


@dataclass(frozen=True)
class RunsOutcome:
    """Result of the ``runs`` job."""

    runs: list[RunSummary]


@dataclass(frozen=True)
class RunDetailOutcome:
    """Result of the ``run_detail`` job: the captures of one run."""

    run_id: str
    captures: list[CaptureSidecar]


@dataclass(frozen=True)
class PresetListOutcome:
    """Result of ``presets_list``."""

    presets: list[PresetRef]


@dataclass(frozen=True)
class SetupOutcome:
    """Result of a ``setup_*`` job: the step result (if any) and what is due next."""

    result: StepResult | None
    next_step: StepName | None
    focus_best: float | None = None


@dataclass(frozen=True)
class FocusOutcome:
    """Result of ``setup_focus_reading``: one live MTF50 reading."""

    mtf50_cyc_px: float | None
    mtf50_lp_mm: float | None
    best: float | None
    message: str = ""


# ============================================================== pure helpers
def preview_to_qimage(rgb: npt.NDArray[np.uint8]) -> QImage:
    """Copy an 8-bit RGB array (HxWx3) into an owning ``QImage``."""
    arr = np.ascontiguousarray(rgb)
    h, w = arr.shape[:2]
    image = QImage(arr.data, w, h, w * CHANNELS_RGB, QImage.Format.Format_RGB888)
    return image.copy()  # own the pixels: ``arr`` goes away


def clip_overlay_image(mask: npt.NDArray[np.bool_]) -> QImage | None:
    """A translucent red RGBA image marking ``mask`` pixels, or ``None`` if nothing is clipped."""
    if not mask.any():
        return None
    h, w = mask.shape
    rgba = np.zeros((h, w, CHANNELS_RGBA), dtype=np.uint8)
    rgba[mask] = OVERLAY_RGBA
    image = QImage(rgba.data, w, h, w * CHANNELS_RGBA, QImage.Format.Format_RGBA8888)
    return image.copy()


def fast_scores(preview: npt.NDArray[np.uint8]) -> dict[str, Any]:
    """Mean luma (0-1), clipped % (any channel at max) and a luma histogram of the preview."""
    luma = (preview.astype(np.float32) @ np.asarray(REC709, dtype=np.float32)) / MAX_PIXEL
    counts, _ = np.histogram(luma, bins=HISTOGRAM_BINS, range=HISTOGRAM_RANGE)
    total = max(1, int(luma.size))
    clipped = (preview == MAX_PIXEL).any(axis=2)
    return {
        "mean": round(float(luma.mean()), STATS_ROUND_DIGITS),
        "clipped_pct": round(PERCENT * float(clipped.mean()), STATS_ROUND_DIGITS),
        "histogram": [float(c) / total for c in counts],
        "clip_mask": clipped,
    }


def build_live_frame(frame: Frame) -> tuple[QImage, dict[str, Any]]:
    """Turn a grabbed frame into ``(preview image, stats dict)`` for ``frame_ready``."""
    preview = make_preview(frame, PREVIEW_LONG_EDGE_PX)
    stats = fast_scores(preview)
    mask = stats.pop("clip_mask")
    stats["clip_overlay"] = clip_overlay_image(mask)
    stats.update(
        frame_id=frame.frame_id,
        width=frame.width,
        height=frame.height,
        pixel_format=frame.pixel_format,
    )
    return preview_to_qimage(preview), stats


def _summary_demosaic(frame: Frame) -> dict[str, Any]:
    out = demosaic(frame.data, frame.pixel_format)
    return {"shape": list(out.shape), "dtype": str(out.dtype)}


def _summary_make_preview(frame: Frame) -> dict[str, Any]:
    return {"shape": list(make_preview(frame, PREVIEW_LONG_EDGE_PX).shape)}


def _summary_to_luma(frame: Frame) -> dict[str, Any]:
    luma = np.asarray(to_display_array(frame), dtype=np.float64) / float(frame.full_scale)
    if luma.ndim == CHANNELS_RGB:
        luma = luma @ np.asarray(REC709)
    return {"mean_luma": round(float(luma.mean()), SUMMARY_ROUND_DIGITS)}


#: How the plugin panel summarises each *registered* processor for a frame.
PROCESSOR_SUMMARIES: dict[str, Callable[[Frame], dict[str, Any]]] = {
    "demosaic": _summary_demosaic,
    "make_preview": _summary_make_preview,
    "to_luma": _summary_to_luma,
}


def processor_results(frame: Frame) -> list[ProcessorResult]:
    """Run the summaries of the registered processors that have one."""
    out: list[ProcessorResult] = []
    for name in PROCESSORS.names():
        summary = PROCESSOR_SUMMARIES.get(name)
        if summary is None:
            continue
        try:
            out.append(ProcessorResult(name=name, result=summary(frame)))
        except Exception as exc:  # a broken processor must not fail the capture
            out.append(ProcessorResult(name=name, result={"error": str(exc)}))
    return out


# ================================================================== worker
class CameraWorker(QObject):
    """Owns every service call. Lives in its own ``QThread``; talk to it with signals."""

    frame_ready = Signal(QImage, dict)
    live_state_changed = Signal(bool)
    live_error = Signal(str)
    job_done = Signal(str, object)
    job_failed = Signal(str, str, str)

    def __init__(self, service: TwoDService, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.service = service
        self._camera_id: str | None = None
        self._live_timer: QTimer | None = None
        self._live = False
        self._errors_in_a_row = 0
        self._frame_times: deque[float] = deque(maxlen=FPS_WINDOW_FRAMES)
        self._measured_fps = 0.0
        self._frames_emitted = 0
        self._handlers: dict[str, Callable[..., object]] = {
            "discover": self._job_discover,
            "open": self._job_open,
            "close": self._job_close,
            "settings": self._job_settings,
            "apply": self._job_apply,
            "set_mode": self._job_set_mode,
            "capture": self._job_capture,
            "tune": self._job_tune,
            "lights_list": self._job_lights_list,
            "lights_set": self._job_lights_set,
            "lights_off": self._job_lights_off,
            "runs": self._job_runs,
            "run_detail": self._job_run_detail,
            "presets_list": self._job_presets_list,
            "preset_save": self._job_preset_save,
            "preset_apply": self._job_preset_apply,
            "live_start": self._job_live_start,
            "live_stop": self._job_live_stop,
            "setup_create": self._job_setup_create,
            "setup_step": self._job_setup_step,
            "setup_focus_reading": self._job_setup_focus_reading,
            "setup_finish_focus": self._job_setup_finish_focus,
            "setup_save": self._job_setup_save,
        }
        self._setup: SetupSession | None = None
        #: Keyword arguments for new :class:`SetupSession` objects (tests: ``sleep``, ``root``).
        self.setup_options: dict[str, Any] = {}
        self._last_stream_emit = 0.0

    # ----------------------------------------------------------- read-outs
    @property
    def measured_fps(self) -> float:
        """Live preview rate measured over the last frames (0 when not live)."""
        return self._measured_fps

    @property
    def frames_emitted(self) -> int:
        """Total number of live frames emitted."""
        return self._frames_emitted

    @property
    def live(self) -> bool:
        """True while the live loop runs."""
        return self._live

    # -------------------------------------------------------------- slots
    @Slot(str, object)
    def run_job(self, name: str, kwargs: object) -> None:
        """Run one named job with its keyword arguments; answer via signals."""
        handler = self._handlers.get(name)
        if handler is None:
            self.job_failed.emit(name, "unknown_job", f"unknown job {name!r}")
            return
        args = kwargs if isinstance(kwargs, dict) else {}
        try:
            payload = handler(**args)
        except CameraBusyError as exc:
            self.job_failed.emit(name, exc.code, str(exc))
        except TwoDError as exc:
            self.job_failed.emit(name, exc.code, str(exc))
        except Exception as exc:
            _log.exception("gui_job_failed", job=name)
            self.job_failed.emit(name, "internal_error", f"{type(exc).__name__}: {exc}")
        else:
            self.job_done.emit(name, payload)

    @Slot()
    def shutdown(self) -> None:
        """Stop live, turn the lights off, close every camera (releases the locks)."""
        self._stop_live()
        try:
            self.service.lights_off(ACTOR)
        except TwoDError as exc:  # no light controller configured
            _log.debug("gui_lights_off_skipped", error=str(exc))
        except Exception:
            _log.exception("gui_lights_off_failed")
        finally:
            self.service.shutdown()
            self._camera_id = None

    # ------------------------------------------------------------- helpers
    def _require_camera(self) -> str:
        if self._camera_id is None:
            raise TwoDError("no camera is open")
        return self._camera_id

    def _view(self) -> SettingsView:
        return self.service.get_settings(self._require_camera())

    def _stop_live(self) -> None:
        was_live = self._live
        self._live = False
        if self._live_timer is not None:
            self._live_timer.stop()
        self._measured_fps = 0.0
        self._frame_times.clear()
        if was_live:
            self.live_state_changed.emit(False)

    # ---------------------------------------------------------------- jobs
    def _job_discover(self) -> list[CameraInfo]:
        return self.service.discover()

    def _job_open(self, camera_id: str) -> OpenOutcome:
        self._stop_live()
        self._setup = None
        if self._camera_id is not None and self._camera_id != camera_id:
            self.service.close(self._camera_id)
            self._camera_id = None
        info = self.service.open(camera_id, LOCK_OWNER)
        self._camera_id = info.serial
        view = self.service.get_settings(info.serial)
        modes: tuple[str, ...] = ()
        default_mode: str | None = None
        try:
            profile = self.service.get_profile(info.serial)
            modes = tuple(profile.modes)
            default_mode = profile.default_mode
        except TwoDError as exc:  # a broken profile must not block the bench
            _log.warning("gui_profile_unavailable", camera_id=info.serial, error=str(exc))
        return OpenOutcome(info, view, modes, self.service.current_mode(info.serial), default_mode)

    def _job_close(self) -> None:
        self._stop_live()
        self._setup = None
        if self._camera_id is not None:
            self.service.close(self._camera_id)
            self._camera_id = None

    def _job_settings(self) -> SettingsView:
        return self._view()

    def _job_apply(self, patch: Any) -> ApplyOutcome:
        cid = self._require_camera()
        result = self.service.apply(cid, patch, ACTOR)
        return ApplyOutcome(result, self.service.get_settings(cid), self.service.current_mode(cid))

    def _job_set_mode(self, mode: str) -> ApplyOutcome:
        cid = self._require_camera()
        result = self.service.set_mode(cid, mode, ACTOR)
        return ApplyOutcome(result, self.service.get_settings(cid), self.service.current_mode(cid))

    def _job_capture(self, note: str = "gui capture") -> CaptureOutcome:
        cid = self._require_camera()
        captures = self.service.capture(cid, 1, note=note, actor=ACTOR)
        frame = self.service.last_frame(cid)
        return CaptureOutcome(captures, processor_results(frame) if frame is not None else [])

    def _job_tune(self, max_steps: int = TUNE_MAX_STEPS) -> TuneOutcome:
        cid = self._require_camera()
        result = self.service.tune(cid, max_steps=max_steps, operator="human")
        return TuneOutcome(result, self.service.get_settings(cid), self.service.current_mode(cid))

    def _job_lights_list(self) -> LightOutcome:
        return LightOutcome(self.service.list_lights())

    def _job_lights_set(self, levels: dict[str, float]) -> LightOutcome:
        res = self.service.set_lights(levels, ACTOR)
        rejected = tuple(r.reason or r.channel for r in res.results if r.status == "rejected")
        return LightOutcome(self.service.list_lights(), rejected)

    def _job_lights_off(self) -> LightOutcome:
        self.service.lights_off(ACTOR)
        return LightOutcome(self.service.list_lights())

    def _job_runs(self, camera_id: str | None = None) -> RunsOutcome:
        return RunsOutcome(self.service.list_runs(camera_id))

    def _job_run_detail(self, run_id: str) -> RunDetailOutcome:
        self.service.get_run(run_id)  # validates the id and that the run exists
        captures_dir = safe_join(two_d_runs_root(), run_id, "captures")
        sidecars: list[CaptureSidecar] = []
        for path in sorted(captures_dir.glob(CAPTURE_JSON_GLOB)):
            try:
                sidecars.append(CaptureSidecar.model_validate_json(path.read_bytes()))
            except (OSError, ValueError) as exc:
                _log.warning("gui_capture_skipped", path=path.name, error=str(exc))
        return RunDetailOutcome(run_id, sidecars)

    def _job_presets_list(self, camera_id: str | None = None) -> PresetListOutcome:
        return PresetListOutcome(self.service.list_presets(camera_id))

    def _job_preset_save(self, name: str, capture_id: str, overwrite: bool = False) -> str:
        cid = self._require_camera()
        path = self.service.save_preset(cid, name, capture_id, overwrite=overwrite)
        return str(path)

    def _job_preset_apply(self, name: str) -> PresetOutcome:
        cid = self._require_camera()
        applied = self.service.apply_preset(cid, name, ACTOR)
        channels = self.service.list_lights() if self.service.light_service else []
        return PresetOutcome(applied, self.service.get_settings(cid), channels)

    # --------------------------------------------------------------- setup
    def _require_setup(self) -> SetupSession:
        if self._setup is None:
            raise InvalidRequestError("no camera setup session: open the setup window first")
        return self._setup

    @contextmanager
    def _streaming_grabs(self) -> Iterator[None]:
        """While a setup step runs, show every frame it grabs (live video keeps moving)."""
        original = self.service.grab

        def grab(camera_id: str, timeout_ms: int = 2000) -> Frame:
            frame = original(camera_id, timeout_ms)
            now = time.monotonic()
            if now - self._last_stream_emit >= LIVE_MIN_INTERVAL_S:
                self._last_stream_emit = now
                try:
                    image, stats = build_live_frame(frame)
                except Exception:  # a preview problem must not fail the step
                    _log.exception("gui_setup_preview_failed")
                else:
                    self._frames_emitted += 1
                    self.frame_ready.emit(image, stats)
            return frame

        had_own = "grab" in vars(self.service)
        self.service.grab = grab  # type: ignore[method-assign]
        try:
            yield
        finally:
            if had_own:
                self.service.grab = original  # type: ignore[method-assign]
            else:
                del self.service.grab

    def _job_setup_create(self) -> SetupOutcome:
        self._setup = SetupSession(self.service, self._require_camera(), **self.setup_options)
        return SetupOutcome(None, self._setup.next_step())

    def _job_setup_step(self, step: str) -> SetupOutcome:
        session = self._require_setup()
        methods: dict[str, Callable[[], StepResult]] = {
            "backup": session.step_backup,
            "light": session.step_light,
            "brightness": session.step_brightness,
            "brightness_recheck": session.step_brightness_recheck,
            "white_balance": session.step_white_balance,
        }
        method = methods.get(step)
        if method is None:
            raise InvalidRequestError(f"{step!r} is not a step the window runs by itself")
        with self._streaming_grabs():
            result = method()
        return SetupOutcome(result, session.next_step(), session.focus_best)

    def _job_setup_focus_reading(self) -> FocusOutcome:
        session = self._require_setup()
        with self._streaming_grabs():
            reading = session.focus_reading()
        return FocusOutcome(
            reading.get("mtf50_cyc_px"),
            reading.get("mtf50_lp_mm"),
            reading.get("best"),
            str(reading.get("message", "")),
        )

    def _job_setup_finish_focus(self, locked: bool) -> SetupOutcome:
        session = self._require_setup()
        with self._streaming_grabs():
            result = session.finish_focus(locked)
        return SetupOutcome(result, session.next_step(), session.focus_best)

    def _job_setup_save(self, confirm: bool, preset_name: str | None = None) -> SetupOutcome:
        session = self._require_setup()
        with self._streaming_grabs():
            result = session.save(confirm, preset_name)
        return SetupOutcome(result, session.next_step(), session.focus_best)

    # ---------------------------------------------------------------- live
    def _job_live_start(self) -> None:
        self._require_camera()
        if self._live_timer is None:
            self._live_timer = QTimer(self)  # created here, so it lives in the worker thread
            self._live_timer.setSingleShot(True)
            self._live_timer.timeout.connect(self._on_live_timer_timeout)
        if self._live:
            return
        self._live = True
        self._errors_in_a_row = 0
        self._frame_times.clear()
        self.live_state_changed.emit(True)
        self._live_timer.start(0)

    def _job_live_stop(self) -> None:
        self._stop_live()

    def _on_live_timer_timeout(self) -> None:
        if not self._live or self._camera_id is None:
            return
        started = time.monotonic()
        try:
            frame = self.service.grab(self._camera_id, LIVE_GRAB_TIMEOUT_MS)
            image, stats = build_live_frame(frame)
        except Exception as exc:
            self._errors_in_a_row += 1
            self.live_error.emit(f"{type(exc).__name__}: {exc}")
            if self._errors_in_a_row >= LIVE_MAX_CONSECUTIVE_ERRORS:
                self._stop_live()
                return
        else:
            self._errors_in_a_row = 0
            now = time.monotonic()
            self._frame_times.append(now)
            if len(self._frame_times) >= MIN_FPS_SAMPLES:
                span = self._frame_times[-1] - self._frame_times[0]
                self._measured_fps = (len(self._frame_times) - 1) / span if span > 0 else 0.0
            stats["fps"] = self._measured_fps
            self._frames_emitted += 1
            self.frame_ready.emit(image, stats)
        if self._live and self._live_timer is not None:
            spare = LIVE_MIN_INTERVAL_S - (time.monotonic() - started)
            self._live_timer.start(max(0, int(spare * MS_PER_S)))
