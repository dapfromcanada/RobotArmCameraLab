"""Driver interface for 2D cameras.

Drivers know **nothing** about safety limits, scoring or files:
``two_d/service.py`` composes them with core. A driver only:

- discovers its own vendor's cameras (and skips every other vendor);
- reports live capabilities (:class:`SettingSpec`) read from the camera;
- applies already-validated settings, rounding to the camera increment and
  reading every value back;
- streams and grabs frames, returning **copies** (buffers are requeued).

Format-type settings (``requires_stream_stop``) must not be changed while
streaming: drivers raise :class:`StreamingStateError` instead.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from types import TracebackType
from typing import TYPE_CHECKING, Any, ClassVar

from camera_lab.two_d.models import (
    ApplyResult,
    CameraInfo,
    CameraSettings,
    CameraSettingsPatch,
    Frame,
    SettingSpec,
)

if TYPE_CHECKING:
    from camera_lab.two_d.backups import RestoreReport


class Camera2D(ABC):
    """An open 2D camera."""

    @property
    @abstractmethod
    def info(self) -> CameraInfo:
        """Camera identity."""

    @property
    @abstractmethod
    def is_streaming(self) -> bool:
        """True while acquisition is running."""

    @abstractmethod
    def capabilities(self) -> dict[str, SettingSpec]:
        """Return the camera's allowed ranges/options, read live, keyed by setting name."""

    @abstractmethod
    def get_settings(self) -> CameraSettings:
        """Read the current settings back from the camera."""

    @abstractmethod
    def apply(self, patch: CameraSettingsPatch) -> ApplyResult:
        """Apply ``patch`` and return per-field results with read-back values."""

    @abstractmethod
    def start_stream(self) -> None:
        """Start acquisition (idempotent)."""

    @abstractmethod
    def stop_stream(self) -> None:
        """Stop acquisition (idempotent)."""

    @abstractmethod
    def grab(self, timeout_ms: int = 2000) -> Frame:
        """Return the next frame (a copy). Raises CaptureTimeoutError / IncompleteFrameError."""

    @abstractmethod
    def close(self) -> None:
        """Stop streaming and release the device (idempotent)."""

    # ------------------------------------------------- backup / restore (Phase 10)
    #: Backup taken by the driver on open, before it configured the camera (or None).
    open_backup: dict[str, Any] | None = None

    def backup_settings(self) -> dict[str, Any]:
        """The whole camera configuration as a JSON-able payload (see two_d/backups.py)."""
        raise NotImplementedError(f"{type(self).__name__} cannot back up settings")

    def restore_settings(
        self, payload: dict[str, Any], protected: Callable[[str], bool] | None = None
    ) -> RestoreReport:
        """Write a :meth:`backup_settings` payload back (stream must be stopped).

        Features for which ``protected(name)`` is true are never written; those
        whose backed-up value differs are listed in ``protected_skipped``.
        """
        raise NotImplementedError(f"{type(self).__name__} cannot restore settings")

    def save_user_set(self, slot: str = "UserSet1", make_default: bool = True) -> dict[str, Any]:
        """Write the current settings to camera memory (``slot``); optionally boot from it.

        Writes camera flash: callers must have the human's explicit confirmation.
        Returns ``{"slot", "default" (set or None), "stored_features" (list or None),
        "notes"}``.
        """
        raise NotImplementedError(f"{type(self).__name__} cannot save user sets")

    def __enter__(self) -> Camera2D:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


class Camera2DDriver(ABC):
    """Discovers and opens one vendor's cameras."""

    #: Registry name (``simulated``, ``arena``, ``ebus``).
    name: ClassVar[str]

    @abstractmethod
    def discover(self) -> list[CameraInfo]:
        """List this driver's cameras."""

    @abstractmethod
    def open(self, camera_id: str) -> Camera2D:
        """Open the camera with serial ``camera_id``. Raises CameraNotFoundError."""
