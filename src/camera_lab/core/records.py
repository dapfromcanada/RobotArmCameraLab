"""Run records: ``run.json``, ``steps.jsonl``, ``captures/`` and ``previews/``.

Layout under ``WS/data/raw_recordings/camera_lab/two_d/<run_id>/``::

    run.json              RunRecord (rewritten atomically on finish)
    steps.jsonl           one StepEntry per line
    captures/NNNN.tiff    raw frame as delivered (uint8/uint16/uint32, uncompressed),
                          sidecar JSON also in the TIFF ImageDescription
    captures/NNNN.json    CaptureSidecar
    previews/NNNN.jpg     8-bit preview, long edge <= 1024 px, JPEG quality 85

``run_id`` = ``YYYYMMDD-HHMMSS_<serial>_<slug>``; ``capture_id`` = ``<run_id>/<NNNN>``.
All files are written atomically (temp file + ``os.replace``) and every path is
built with :func:`~camera_lab.core.paths.safe_join`. There is no delete function.

Core does not depend on the two_d models: camera info, settings and scores are
passed in as plain mappings (or pydantic models, which are dumped to JSON).
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import tempfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any, Literal

import cv2
import numpy as np
import numpy.typing as npt
import tifffile
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from camera_lab.core.logging import get_logger
from camera_lab.core.paths import (
    SERIAL_RE,
    SLUG_RE,
    safe_join,
    slugify,
    two_d_runs_root,
    validate_serial,
)

SCHEMA_VERSION: Literal[1] = 1
RUN_ID_RE = re.compile(rf"\d{{8}}-\d{{6}}_({SERIAL_RE.pattern})_({SLUG_RE.pattern})")
CAPTURE_ID_RE = re.compile(rf"{RUN_ID_RE.pattern}/\d{{4}}")
MAX_CAPTURES_PER_RUN = 9999  # NNNN numbering
PREVIEW_MAX_EDGE = 1024
PREVIEW_JPEG_QUALITY = 85

_log = get_logger(__name__)


class RecordError(RuntimeError):
    """A run record could not be created, read or written."""


class RunNotFoundError(RecordError):
    """No run (or capture) with that id."""


class CaptureLimitError(RecordError):
    """The run already holds its maximum number of captures."""


def utc_now() -> datetime:
    """Return the current time, timezone-aware UTC."""
    return datetime.now(UTC)


def _as_utc(v: datetime) -> datetime:
    if v.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return v.astimezone(UTC)


# ===================================================================== models
class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class SoftwareInfo(_Model):
    """Software versions recorded in ``run.json``."""

    camera_lab: str
    sdk_name: str | None = None
    sdk_version: str | None = None
    firmware: str | None = None


class RunRecord(_Model):
    """``run.json``."""

    schema_version: Literal[1] = SCHEMA_VERSION
    run_id: str = Field(pattern=f"^{RUN_ID_RE.pattern}$")
    camera: dict[str, Any]
    method: Literal["agent", "classic", "manual", "setup"]
    operator: Literal["human", "agent"]
    goal: str = Field(default="", max_length=2000)
    started_at: datetime
    finished_at: datetime | None = None
    max_steps: int = Field(ge=0)
    steps_used: int = Field(default=0, ge=0)
    best_capture_id: str | None = Field(default=None, pattern=f"^{CAPTURE_ID_RE.pattern}$")
    preset_saved: str | None = None
    summary: str = Field(default="", max_length=4000)
    #: Ids of the test charts seen in any capture of this run (Phase 10).
    charts_seen: list[str] = Field(default_factory=list, max_length=256)
    software: SoftwareInfo
    limits_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("started_at", "finished_at")
    @classmethod
    def _utc(cls, v: datetime | None) -> datetime | None:
        return None if v is None else _as_utc(v)

    @field_validator("camera")
    @classmethod
    def _camera_has_serial(cls, v: dict[str, Any]) -> dict[str, Any]:
        serial = v.get("serial")
        if not isinstance(serial, str) or SERIAL_RE.fullmatch(serial) is None:
            raise ValueError("camera.serial missing or invalid")
        return v


class CaptureSidecar(_Model):
    """``captures/NNNN.json`` (also stored in the TIFF ImageDescription)."""

    schema_version: Literal[1] = SCHEMA_VERSION
    capture_id: str = Field(pattern=f"^{CAPTURE_ID_RE.pattern}$")
    timestamp_utc: datetime
    settings: dict[str, Any]
    lights: dict[str, float]
    pixel_format: str = Field(min_length=1, max_length=64)
    bit_depth: int = Field(ge=1, le=24)
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    channels: Literal[1, 3]
    dtype: Literal["uint8", "uint16", "uint32"]
    frame_id: int | None = None
    scores: dict[str, Any] = Field(default_factory=dict)
    preview: str | None = None
    note: str = Field(default="", max_length=500)
    #: Test charts seen in this capture (Phase 10): chart id, version, tags, homography.
    charts: list[dict[str, Any]] = Field(default_factory=list, max_length=64)

    @field_validator("timestamp_utc")
    @classmethod
    def _utc(cls, v: datetime) -> datetime:
        return _as_utc(v)


class StepEntry(_Model):
    """One line of ``steps.jsonl``."""

    timestamp: datetime
    actor: str = Field(min_length=1, max_length=32)
    tool: str = Field(min_length=1, max_length=64)
    arguments: dict[str, Any] = Field(default_factory=dict)
    result: Any = None
    rejections: list[str] = Field(default_factory=list)


@dataclass(frozen=True)
class RunSummary:
    """One row of :func:`list_runs` (for the run browser)."""

    run_id: str
    camera_serial: str
    method: str
    operator: str
    started_at: datetime
    finished_at: datetime | None
    capture_count: int
    best_capture_id: str | None
    best_score: float | None


# ============================================================== atomic files
@contextmanager
def _atomic_open(path: Path) -> Iterator[IO[bytes]]:
    """Write to a temp file next to ``path``, fsync, then ``os.replace`` it into place."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            yield fh
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Atomically write ``data`` to ``path``."""
    with _atomic_open(path) as fh:
        fh.write(data)


def _to_jsonable(obj: Any) -> Any:
    if isinstance(obj, BaseModel):
        return obj.model_dump(mode="json")
    if isinstance(obj, Mapping):
        return dict(obj)
    return obj


def _json_bytes(model: BaseModel) -> bytes:
    return model.model_dump_json(indent=2).encode("utf-8") + b"\n"


# ================================================================== helpers
def parse_capture_id(capture_id: str) -> tuple[str, int]:
    """Split ``<run_id>/<NNNN>`` into ``(run_id, n)``; raises ``ValueError`` if invalid."""
    if not isinstance(capture_id, str) or CAPTURE_ID_RE.fullmatch(capture_id) is None:
        raise ValueError(f"invalid capture id {capture_id!r}")
    run_id, n = capture_id.rsplit("/", 1)
    return run_id, int(n)


def _check_image(image: Any) -> npt.NDArray[Any]:
    if not isinstance(image, np.ndarray):
        raise TypeError("image must be a numpy array")
    if image.dtype not in (np.uint8, np.uint16, np.uint32):
        raise TypeError(f"image dtype must be uint8, uint16 or uint32, got {image.dtype}")
    if image.ndim == 2 or (image.ndim == 3 and image.shape[2] == 3):
        if image.size == 0:
            raise ValueError("image is empty")
        return image
    raise ValueError(f"image shape must be HxW or HxWx3, got {image.shape}")


def _encode_preview(preview: npt.NDArray[Any]) -> bytes:
    if not isinstance(preview, np.ndarray) or preview.dtype != np.uint8:
        raise TypeError("preview must be a uint8 numpy array")
    if not (preview.ndim == 2 or (preview.ndim == 3 and preview.shape[2] == 3)):
        raise ValueError(f"preview shape must be HxW or HxWx3, got {preview.shape}")
    h, w = preview.shape[:2]
    scale = PREVIEW_MAX_EDGE / max(h, w)
    if scale < 1.0:
        size = (max(1, round(w * scale)), max(1, round(h * scale)))
        preview = cv2.resize(preview, size, interpolation=cv2.INTER_AREA)
    if preview.ndim == 3:
        preview = cv2.cvtColor(preview, cv2.COLOR_RGB2BGR)
    ok, buf = cv2.imencode(".jpg", preview, [cv2.IMWRITE_JPEG_QUALITY, PREVIEW_JPEG_QUALITY])
    if not ok:
        raise RecordError("JPEG encoding of the preview failed")
    return bytes(buf.tobytes())


# ================================================================ recorder
class RunRecorder:
    """Creates and appends to one run folder."""

    def __init__(
        self,
        run_dir: Path,
        record: RunRecord,
        *,
        max_captures: int = MAX_CAPTURES_PER_RUN,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self.run_dir = run_dir
        self._record = record
        self.max_captures = min(max_captures, MAX_CAPTURES_PER_RUN)
        self._now = now
        self._count = len(self._existing_capture_numbers())

    # ----------------------------------------------------------- create/load
    @classmethod
    def create(
        cls,
        camera_info: Mapping[str, Any] | BaseModel,
        method: Literal["agent", "classic", "manual", "setup"],
        operator: Literal["human", "agent"],
        goal: str,
        max_steps: int,
        *,
        software: SoftwareInfo,
        limits_hash: str,
        slug_source: str | None = None,
        max_captures: int = MAX_CAPTURES_PER_RUN,
        root: Path | None = None,
        now: Callable[[], datetime] = utc_now,
    ) -> RunRecorder:
        """Create a new run folder and write the initial ``run.json``.

        The slug comes from ``slug_source`` (or ``goal``), else ``run``. If the
        folder already exists (same second), ``-2``, ``-3`` ... is appended.
        """
        camera = _to_jsonable(camera_info)
        serial = validate_serial(str(camera.get("serial", "")))
        root = Path(root) if root is not None else two_d_runs_root()
        root.mkdir(parents=True, exist_ok=True)
        started = now()
        base_slug = slugify(slug_source if slug_source is not None else goal)
        for attempt in range(1, 100):
            suffix = "" if attempt == 1 else f"-{attempt}"
            slug = base_slug[: 40 - len(suffix)].rstrip("-") + suffix
            run_id = f"{started:%Y%m%d-%H%M%S}_{serial}_{slug}"
            run_dir = safe_join(root, run_id)
            try:
                run_dir.mkdir()
            except FileExistsError:
                continue
            break
        else:
            raise RecordError(f"could not create a unique run folder for {serial}")
        try:
            record = RunRecord(
                run_id=run_id,
                camera=camera,
                method=method,
                operator=operator,
                goal=goal,
                started_at=started,
                max_steps=max_steps,
                software=software,
                limits_hash=limits_hash,
            )
        except ValidationError:
            run_dir.rmdir()  # still empty: we created it a moment ago
            raise
        safe_join(run_dir, "captures").mkdir()
        safe_join(run_dir, "previews").mkdir()
        safe_join(run_dir, "steps.jsonl").touch()
        rec = cls(run_dir, record, max_captures=max_captures, now=now)
        rec._write_run_json()
        _log.info("run_created", run_id=run_id, method=method, operator=operator)
        return rec

    @classmethod
    def load(
        cls,
        run_id: str,
        *,
        root: Path | None = None,
        max_captures: int = MAX_CAPTURES_PER_RUN,
        now: Callable[[], datetime] = utc_now,
    ) -> RunRecorder:
        """Open an existing run (to add captures or finish it)."""
        run_dir = _run_dir(run_id, root)
        return cls(run_dir, _read_run_json(run_dir), max_captures=max_captures, now=now)

    # ------------------------------------------------------------ properties
    @property
    def run_id(self) -> str:
        """The run id."""
        return self._record.run_id

    @property
    def record(self) -> RunRecord:
        """A copy of the current ``run.json`` contents."""
        return self._record.model_copy(deep=True)

    @property
    def capture_count(self) -> int:
        """Captures recorded so far."""
        return self._count

    # --------------------------------------------------------------- writing
    def _write_run_json(self) -> None:
        atomic_write_bytes(safe_join(self.run_dir, "run.json"), _json_bytes(self._record))

    def _existing_capture_numbers(self) -> list[int]:
        cap_dir = self.run_dir / "captures"
        if not cap_dir.is_dir():
            return []
        return sorted(int(p.stem) for p in cap_dir.glob("[0-9][0-9][0-9][0-9].json") if p.is_file())

    def add_capture(
        self,
        image: npt.NDArray[Any],
        *,
        pixel_format: str,
        bit_depth: int,
        settings: Mapping[str, Any] | BaseModel,
        lights: Mapping[str, float],
        scores: Mapping[str, Any] | BaseModel | None = None,
        frame_id: int | None = None,
        preview: npt.NDArray[Any] | None = None,
        note: str = "",
        charts: list[dict[str, Any]] | None = None,
    ) -> str:
        """Write one capture (TIFF + sidecar, optional preview); return its capture id.

        Raises:
            CaptureLimitError: the run already has ``max_captures`` captures.
        """
        img = _check_image(image)
        if self._count >= self.max_captures:
            raise CaptureLimitError(
                f"run {self.run_id} already has {self._count} captures (limit {self.max_captures})"
            )
        n = self._count + 1
        name = f"{n:04d}"
        capture_id = f"{self.run_id}/{name}"
        preview_rel = None
        if preview is not None:
            preview_rel = f"previews/{name}.jpg"
            atomic_write_bytes(
                safe_join(self.run_dir, "previews", f"{name}.jpg"), _encode_preview(preview)
            )
        sidecar = CaptureSidecar(
            capture_id=capture_id,
            timestamp_utc=self._now(),
            settings=_to_jsonable(settings),
            lights=dict(lights),
            pixel_format=pixel_format,
            bit_depth=bit_depth,
            width=img.shape[1],
            height=img.shape[0],
            channels=1 if img.ndim == 2 else 3,
            dtype=str(img.dtype),  # type: ignore[arg-type]
            frame_id=frame_id,
            scores=_to_jsonable(scores) if scores is not None else {},
            preview=preview_rel,
            note=note,
            charts=_to_jsonable(charts) if charts else [],
        )
        # TIFF ImageDescription must be ASCII: escape anything else.
        description = json.dumps(sidecar.model_dump(mode="json"), ensure_ascii=True)
        buf = io.BytesIO()
        tifffile.imwrite(
            buf,
            img,
            photometric="minisblack" if img.ndim == 2 else "rgb",
            compression=None,
            description=description,
            metadata=None,
        )
        atomic_write_bytes(safe_join(self.run_dir, "captures", f"{name}.tiff"), buf.getvalue())
        atomic_write_bytes(
            safe_join(self.run_dir, "captures", f"{name}.json"), _json_bytes(sidecar)
        )
        self._count = n
        seen = {c["chart_id"] for c in sidecar.charts if c.get("chart_id")}
        if seen - set(self._record.charts_seen):
            charts_seen = sorted(set(self._record.charts_seen) | seen)
            self._record = self._record.model_copy(update={"charts_seen": charts_seen})
            self._write_run_json()
        _log.info("capture_saved", capture_id=capture_id)
        return capture_id

    def log_step(
        self,
        actor: str,
        tool: str,
        arguments: Mapping[str, Any] | None = None,
        result: Any = None,
        rejections: Sequence[str] = (),
    ) -> StepEntry:
        """Append one line to ``steps.jsonl``."""
        entry = StepEntry(
            timestamp=self._now(),
            actor=actor,
            tool=tool,
            arguments=dict(arguments or {}),
            result=_to_jsonable(result),
            rejections=list(rejections),
        )
        line = entry.model_dump_json() + "\n"
        with safe_join(self.run_dir, "steps.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(line)
            fh.flush()
        return entry

    def finish(
        self,
        *,
        best_capture_id: str | None,
        summary: str = "",
        preset_saved: str | None = None,
        steps_used: int | None = None,
    ) -> RunRecord:
        """Set the final fields and rewrite ``run.json``.

        Raises:
            RecordError: if the run is already finished or the best capture is unknown.
        """
        if self._record.finished_at is not None:
            raise RecordError(f"run {self.run_id} is already finished")
        if best_capture_id is not None:
            run_id, n = parse_capture_id(best_capture_id)
            if run_id != self.run_id or n not in self._existing_capture_numbers():
                raise RecordError(f"best capture {best_capture_id!r} is not in run {self.run_id}")
        update: dict[str, Any] = {
            "finished_at": self._now(),
            "best_capture_id": best_capture_id,
            "summary": summary,
            "preset_saved": preset_saved,
        }
        if steps_used is not None:
            update["steps_used"] = steps_used
        self._record = RunRecord.model_validate({**self._record.model_dump(), **update})
        self._write_run_json()
        _log.info("run_finished", run_id=self.run_id, best_capture_id=best_capture_id)
        return self.record

    def read_capture(self, capture_id: str) -> tuple[npt.NDArray[Any], CaptureSidecar]:
        """Read one capture of this run (see :func:`read_capture`)."""
        run_id, _ = parse_capture_id(capture_id)
        if run_id != self.run_id:
            raise RunNotFoundError(f"capture {capture_id!r} is not in run {self.run_id}")
        return read_capture(capture_id, root=self.run_dir.parent)

    def read_steps(self) -> list[StepEntry]:
        """Return all entries of ``steps.jsonl``."""
        text = safe_join(self.run_dir, "steps.jsonl").read_text(encoding="utf-8")
        return [StepEntry.model_validate_json(line) for line in text.splitlines() if line.strip()]


# ================================================================== readers
def _run_dir(run_id: str, root: Path | None) -> Path:
    if not isinstance(run_id, str) or RUN_ID_RE.fullmatch(run_id) is None:
        raise RunNotFoundError(f"invalid run id {run_id!r}")
    run_dir = safe_join(Path(root) if root is not None else two_d_runs_root(), run_id)
    if not run_dir.is_dir():
        raise RunNotFoundError(f"run {run_id} not found")
    return run_dir


def _read_run_json(run_dir: Path) -> RunRecord:
    try:
        return RunRecord.model_validate_json(safe_join(run_dir, "run.json").read_bytes())
    except FileNotFoundError:
        raise RunNotFoundError(f"run {run_dir.name} has no run.json") from None
    except ValidationError as exc:
        raise RecordError(
            f"run {run_dir.name}: invalid run.json: {exc.error_count()} errors"
        ) from None


def load_run(run_id: str, *, root: Path | None = None) -> RunRecord:
    """Return the ``run.json`` of ``run_id``."""
    return _read_run_json(_run_dir(run_id, root))


def read_capture(
    capture_id: str, *, root: Path | None = None
) -> tuple[npt.NDArray[Any], CaptureSidecar]:
    """Return ``(image, sidecar)`` for ``<run_id>/<NNNN>``."""
    try:
        run_id, n = parse_capture_id(capture_id)
    except ValueError as exc:
        raise RunNotFoundError(str(exc)) from None
    run_dir = _run_dir(run_id, root)
    name = f"{n:04d}"
    tiff = safe_join(run_dir, "captures", f"{name}.tiff")
    side = safe_join(run_dir, "captures", f"{name}.json")
    if not tiff.is_file() or not side.is_file():
        raise RunNotFoundError(f"capture {capture_id} not found")
    sidecar = CaptureSidecar.model_validate_json(side.read_bytes())
    image = tifffile.imread(tiff)
    return np.asarray(image), sidecar


def list_runs(
    *,
    root: Path | None = None,
    camera: str | None = None,
    method: str | None = None,
) -> list[RunSummary]:
    """List runs, newest first, optionally filtered by camera serial and method.

    Folders that are not valid runs are skipped (and logged).
    """
    base = Path(root) if root is not None else two_d_runs_root()
    if not base.is_dir():
        return []
    out: list[RunSummary] = []
    for d in base.iterdir():
        if not d.is_dir() or d.is_symlink() or RUN_ID_RE.fullmatch(d.name) is None:
            continue
        try:
            rec = _read_run_json(d)
        except RecordError as exc:
            _log.warning("run_skipped", run=d.name, error=str(exc))
            continue
        serial = str(rec.camera.get("serial"))
        if camera is not None and serial != camera:
            continue
        if method is not None and rec.method != method:
            continue
        caps = sorted((d / "captures").glob("[0-9][0-9][0-9][0-9].json"))
        best_score = None
        if rec.best_capture_id is not None:
            best_side = d / "captures" / f"{rec.best_capture_id.rsplit('/', 1)[1]}.json"
            try:
                comp = CaptureSidecar.model_validate_json(best_side.read_bytes()).scores.get(
                    "composite"
                )
                best_score = float(comp) if isinstance(comp, int | float) else None
            except (OSError, ValidationError):
                best_score = None
        out.append(
            RunSummary(
                run_id=rec.run_id,
                camera_serial=serial,
                method=rec.method,
                operator=rec.operator,
                started_at=rec.started_at,
                finished_at=rec.finished_at,
                capture_count=len(caps),
                best_capture_id=rec.best_capture_id,
                best_score=best_score,
            )
        )
    return sorted(out, key=lambda s: s.run_id, reverse=True)
