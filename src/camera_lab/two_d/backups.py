"""Camera settings backups (Phase 10 step 4).

Before camera_lab changes a camera for the first time in a session, the whole
camera configuration is saved to ``WS/config/camera_backups/<serial>/<id>.json``
(never overwritten; identical consecutive backups are stored once). A backup
can be written back with :meth:`TwoDService.restore_backup`.

The ``payload`` is driver-specific (Arena: the SDK's streamable-features file
plus the features it leaves out, such as the HDR channels; eBUS: a GenICam walk
of the streamable features with selectors expanded; simulated: its settings).

:func:`multipass_restore` is the shared write-back: features are written in
backup order, skipping those already equal; features that are not writable yet
(their enabling feature comes later, e.g. ExposureTime after HDR is switched
off) are retried in up to ``RESTORE_PASSES`` passes. Features that were not
writable when the backup was made are informational and never written.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import re
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from camera_lab.core.paths import camera_backups_root, safe_join, validate_serial
from camera_lab.core.records import atomic_write_bytes

BACKUP_ID_RE = re.compile(r"\d{8}T\d{6}Z(?:-\d{1,3})?")
RESTORE_PASSES = 3
MAX_BACKUP_BYTES = 4_000_000


class BackupError(RuntimeError):
    """A backup could not be made, found or read."""


class FeatureValue(BaseModel):
    """One feature value in a backup (``selector`` set for selector-dependent features)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1, max_length=128)
    value: bool | int | float | str
    writable: bool
    selector: str | None = None
    selector_value: str | int | None = None

    @property
    def key(self) -> str:
        return (
            self.name
            if self.selector is None
            else f"{self.name}[{self.selector}={self.selector_value}]"
        )


class CameraBackup(BaseModel):
    """``<backup_id>.json``."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    backup_id: str = Field(pattern=f"^{BACKUP_ID_RE.pattern}$")
    serial: str
    model: str = ""
    vendor: str = ""
    firmware: str = ""
    driver: str
    created_utc: datetime
    reason: str = Field(default="", max_length=300)
    #: camera_lab's view of the settings at backup time (empty if not yet readable).
    settings: dict[str, Any] = Field(default_factory=dict)
    payload: dict[str, Any]
    payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class RestoreReport(BaseModel):
    """What a restore wrote, skipped and could not set."""

    model_config = ConfigDict(frozen=True)

    written: int = 0
    unchanged: int = 0
    informational: int = 0
    failed: tuple[dict[str, str], ...] = ()
    differences: tuple[str, ...] = ()
    #: Protected features (safety_limits.yaml) whose backed-up value differs: not written.
    protected_skipped: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.failed and not self.differences


def payload_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


class BackupStore:
    """Backups on disk, one folder per camera serial."""

    def __init__(self, root: Path | None = None, now: Callable[[], datetime] | None = None) -> None:
        self._root = root
        self._now = now or (lambda: datetime.now(UTC))

    @property
    def root(self) -> Path:
        return self._root if self._root is not None else camera_backups_root()

    def _dir(self, serial: str) -> Path:
        return safe_join(self.root, validate_serial(serial))

    def list(self, serial: str) -> list[CameraBackup]:
        """Backups of ``serial``, oldest first."""
        d = self._dir(serial)
        if not d.is_dir():
            return []
        paths = [p for p in d.glob("*.json") if BACKUP_ID_RE.fullmatch(p.stem)]
        paths.sort(key=lambda p: (p.stem[:16], int(p.stem[17:] or 0)))  # …Z before …Z-1
        return [self._read(p) for p in paths]

    def load(self, serial: str, backup_id: str) -> CameraBackup:
        """One backup; the id is validated and must belong to ``serial``."""
        if not isinstance(backup_id, str) or not BACKUP_ID_RE.fullmatch(backup_id):
            raise BackupError(f"invalid backup id {backup_id!r}")
        path = safe_join(self._dir(serial), f"{backup_id}.json")
        if not path.is_file():
            raise BackupError(f"no backup {backup_id} for camera {serial}")
        backup = self._read(path)
        if backup.serial != serial or backup.backup_id != backup_id:
            raise BackupError(f"{path} does not belong to camera {serial}")
        if payload_hash(backup.payload) != backup.payload_sha256:
            raise BackupError(f"{path}: payload checksum mismatch (file edited or damaged)")
        return backup

    @staticmethod
    def _read(path: Path) -> CameraBackup:
        raw = path.read_bytes()
        if len(raw) > MAX_BACKUP_BYTES:
            raise BackupError(f"{path}: larger than {MAX_BACKUP_BYTES} bytes")
        try:
            return CameraBackup.model_validate_json(raw)
        except ValueError as exc:
            raise BackupError(f"{path}: invalid backup ({str(exc)[:200]})") from None

    def save(
        self,
        *,
        serial: str,
        driver: str,
        payload: dict[str, Any],
        reason: str,
        settings: dict[str, Any] | None = None,
        model: str = "",
        vendor: str = "",
        firmware: str = "",
    ) -> tuple[CameraBackup, bool]:
        """Store a backup; returns (backup, created). An identical latest backup is reused."""
        digest = payload_hash(payload)
        existing = self.list(serial)
        if existing and existing[-1].payload_sha256 == digest:
            return existing[-1], False
        d = self._dir(serial)
        d.mkdir(parents=True, exist_ok=True)
        base = self._now().astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
        backup_id, n = base, 1
        while safe_join(d, f"{backup_id}.json").exists():
            backup_id, n = f"{base}-{n}", n + 1
        backup = CameraBackup(
            backup_id=backup_id,
            serial=serial,
            model=model,
            vendor=vendor,
            firmware=firmware,
            driver=driver,
            created_utc=self._now(),
            reason=reason[:300],
            settings=settings or {},
            payload=payload,
            payload_sha256=digest,
        )
        data = json.dumps(backup.model_dump(mode="json"), indent=1, sort_keys=True).encode()
        atomic_write_bytes(safe_join(d, f"{backup_id}.json"), data)
        return backup, True


# ------------------------------------------------------------- write-back
def same_value(a: Any, b: Any) -> bool:
    """Equality with a relative tolerance for floats (cameras quantise)."""
    if isinstance(a, bool) or isinstance(b, bool):
        return (
            bool(a) == bool(b) if isinstance(a, bool) and isinstance(b, bool) else str(a) == str(b)
        )
    if isinstance(a, int | float) and isinstance(b, int | float):
        return math.isclose(float(a), float(b), rel_tol=1e-6, abs_tol=1e-9)
    return str(a) == str(b)


class FeatureAccess:
    """How :func:`multipass_restore` reads and writes one SDK's features."""

    def select(self, selector: str, value: str | int) -> None:
        raise NotImplementedError

    def read(self, name: str) -> Any:
        raise NotImplementedError

    def writable(self, name: str) -> bool:
        raise NotImplementedError

    def write(self, name: str, value: Any) -> None:
        raise NotImplementedError


def multipass_restore(features: Sequence[FeatureValue], access: FeatureAccess) -> RestoreReport:
    """Write ``features`` back (see the module docstring)."""
    written = unchanged = 0
    informational = sum(1 for f in features if not f.writable)
    pending = [f for f in features if f.writable]
    errors: dict[str, str] = {}
    for _ in range(RESTORE_PASSES):
        retry: list[FeatureValue] = []
        for f in pending:
            try:
                if f.selector is not None and f.selector_value is not None:
                    access.select(f.selector, f.selector_value)
                if same_value(access.read(f.name), f.value):
                    unchanged += 1
                    errors.pop(f.key, None)
                    continue
                if not access.writable(f.name):
                    errors[f.key] = "not writable in the current camera state"
                    retry.append(f)
                    continue
                access.write(f.name, f.value)
                written += 1
                errors.pop(f.key, None)
            except Exception as exc:  # SDK errors: retry, then report
                errors[f.key] = str(exc).strip()[:200] or type(exc).__name__
                retry.append(f)
        if len(retry) == len(pending):
            pending = retry
            break
        pending = retry
    # selectors back to their backed-up values (the expansion moved them)
    for f in features:
        if f.selector is None and f.writable and f.name in {g.selector for g in features}:
            try:
                access.write(f.name, f.value)
            except Exception as exc:
                errors.setdefault(f.key, str(exc).strip()[:200])
    differences = []
    for f in features:
        if not f.writable:
            continue
        try:
            if f.selector is not None and f.selector_value is not None:
                access.select(f.selector, f.selector_value)
            now = access.read(f.name)
        except Exception as exc:
            errors.setdefault(f.key, str(exc).strip()[:200])
            continue
        if not same_value(now, f.value):
            differences.append(f"{f.key}: backup {f.value!r}, camera {now!r}")
    for f in features:  # leave selectors as backed up
        if f.selector is None and f.name in {g.selector for g in features}:
            with contextlib.suppress(Exception):  # best effort; reported above if it matters
                access.write(f.name, f.value)
    failed = tuple({"feature": k, "reason": v} for k, v in sorted(errors.items()))
    return RestoreReport(
        written=written,
        unchanged=unchanged,
        informational=informational,
        failed=failed,
        differences=tuple(differences[:50]),
    )
