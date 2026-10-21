"""Per-camera process lock: one process controls a camera at a time.

SECURITY-SENSITIVE. The lock is an ``fcntl.flock`` (exclusive, non-blocking) on
``$XDG_RUNTIME_DIR/camera_lab/<serial>.lock``, falling back to
``/tmp/camera_lab-<uid>/<serial>.lock``. The file holds ``<pid> <owner>`` of the
current holder. The kernel releases the lock when the holder's process exits,
so a crashed process never leaves a stale lock (a stale *file* is harmless).

Hardening:
- the lock directory is created ``0700``; an existing directory must be a real
  directory (not a symlink) owned by this user and not writable by others;
- the serial is validated and the path built with :func:`safe_join`;
- the lock file is opened with ``O_NOFOLLOW`` and mode ``0600``;
- the holder text read back is length-limited and sanitised before display.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import re
import stat
from pathlib import Path
from types import TracebackType

from camera_lab.core.paths import UnsafePathError, safe_join, validate_serial

#: Front-end name written into the lock (``cli``, ``mcp``, ``gui``, ``test`` ...).
OWNER_RE = re.compile(r"[a-z0-9_-]{1,32}")
_MAX_HOLDER_BYTES = 128


class CameraBusyError(RuntimeError):
    """Another process (or another open in this process) holds the camera lock."""

    def __init__(self, serial: str, holder: str) -> None:
        self.serial = serial
        self.holder = holder
        super().__init__(f"camera {serial} is busy: locked by {holder}")


class LockDirError(RuntimeError):
    """The lock directory is missing, insecure or cannot be created."""


def lock_dir() -> Path:
    """Return (and create, ``0700``) the directory holding the lock files."""
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime and Path(runtime).is_absolute() and Path(runtime).is_dir():
        d = Path(runtime) / "camera_lab"
    else:
        d = Path("/tmp") / f"camera_lab-{os.getuid()}"  # noqa: S108 - checked below
    try:
        d.mkdir(mode=0o700, exist_ok=True)
    except OSError as exc:
        raise LockDirError(f"cannot create lock dir {d}: {exc.strerror}") from None
    st = os.lstat(d)
    if not stat.S_ISDIR(st.st_mode):
        raise LockDirError(f"lock dir {d} is not a directory (symlink?)")
    if st.st_uid != os.getuid():
        raise LockDirError(f"lock dir {d} is not owned by uid {os.getuid()}")
    if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise LockDirError(f"lock dir {d} is writable by group/others")
    return d


def lock_path(serial: str) -> Path:
    """Return the lock-file path for ``serial`` (validated)."""
    validate_serial(serial)
    return safe_join(lock_dir(), f"{serial}.lock")


def _sanitize_holder(raw: bytes) -> str:
    text = raw[:_MAX_HOLDER_BYTES].decode("ascii", errors="replace").strip()
    text = re.sub(r"[^A-Za-z0-9_ .:-]", "?", text)
    return text or "unknown holder"


class CameraLock:
    """Context manager holding the exclusive lock for one camera.

    Example::

        with CameraLock("U330439", owner="cli"):
            ...  # open and use the camera

    Raises:
        CameraBusyError: the lock is held elsewhere; ``holder`` names it.
        UnsafePathError: the serial or owner is invalid.
    """

    def __init__(self, serial: str, owner: str) -> None:
        self.serial = validate_serial(serial)
        if not isinstance(owner, str) or OWNER_RE.fullmatch(owner) is None:
            raise UnsafePathError(f"invalid lock owner {owner!r}: must match {OWNER_RE.pattern}")
        self.owner = owner
        self._fd: int | None = None

    @property
    def held(self) -> bool:
        """True while this object holds the lock."""
        return self._fd is not None

    def acquire(self) -> CameraLock:
        """Take the lock (non-blocking) and record ``<pid> <owner>`` in the file."""
        if self._fd is not None:
            raise RuntimeError(f"lock for {self.serial} already held by this object")
        path = lock_path(self.serial)
        flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC
        try:
            fd = os.open(path, flags, 0o600)
        except OSError as exc:
            raise LockDirError(f"cannot open lock file {path}: {exc.strerror}") from None
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            try:
                holder = _sanitize_holder(os.pread(fd, _MAX_HOLDER_BYTES, 0))
            finally:
                os.close(fd)
            raise CameraBusyError(self.serial, holder) from None
        except BaseException:
            os.close(fd)
            raise
        os.ftruncate(fd, 0)
        os.pwrite(fd, f"pid {os.getpid()} {self.owner}\n".encode("ascii"), 0)
        self._fd = fd
        return self

    def release(self) -> None:
        """Release the lock. Safe to call more than once."""
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            os.ftruncate(fd, 0)
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def __enter__(self) -> CameraLock:
        return self.acquire()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()

    def __del__(self) -> None:  # pragma: no cover - best effort
        fd = getattr(self, "_fd", None)
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)
