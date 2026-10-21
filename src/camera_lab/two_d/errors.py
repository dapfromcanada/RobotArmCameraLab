"""2D errors. Each carries a stable ``code`` (for MCP) and a CLI ``exit_code``."""

from __future__ import annotations


class TwoDError(Exception):
    """Base class for two_d errors."""

    code = "error"
    exit_code = 1


class CameraNotFoundError(TwoDError):
    """No camera with that id."""

    code = "camera_not_found"
    exit_code = 3


class SdkUnavailableError(TwoDError):
    """A vendor SDK (arena_api / ebus_*) is not installed or failed to load."""

    code = "sdk_unavailable"
    exit_code = 5


class CaptureTimeoutError(TwoDError):
    """No frame arrived within the timeout."""

    code = "capture_timeout"
    exit_code = 6


class IncompleteFrameError(CaptureTimeoutError):
    """A frame arrived incomplete (missing packets)."""

    code = "incomplete_frame"


class StreamingStateError(TwoDError):
    """The operation is not allowed in the current stream state.

    Raised when a format-type setting (pixel format, ROI size, ...) is changed
    while streaming, or when grabbing while not streaming.
    """

    code = "stream_state"
    exit_code = 1


class CameraClosedError(TwoDError):
    """The camera handle has been closed."""

    code = "camera_closed"
    exit_code = 1


class CameraNotOpenError(TwoDError):
    """The camera is not open in this process (call ``TwoDService.open()`` first)."""

    code = "camera_not_open"
    exit_code = 3


class CameraBusyError(TwoDError):
    """Another process (or front end) holds the camera lock."""

    code = "camera_busy"
    exit_code = 3

    def __init__(self, message: str, holder: str = "") -> None:
        super().__init__(message)
        self.holder = holder


class LimitRejectedError(TwoDError):
    """A request was rejected by the safety limits or the camera range."""

    code = "rejected_by_limits"
    exit_code = 4


class StepBudgetExhausted(LimitRejectedError):  # noqa: N818 - name fixed by the spec
    """The tuning run has used all its steps (or captures)."""

    code = "step_budget_exhausted"


class InvalidRequestError(TwoDError):
    """Bad argument value (unknown run/capture/preset, bad file, ...)."""

    code = "invalid_request"
    exit_code = 2


class NotFoundError(InvalidRequestError):
    """A run, capture or preset does not exist."""

    code = "not_found"


class AlreadyExistsError(InvalidRequestError):
    """A preset exists and overwrite was not requested, or a run is already active."""

    code = "already_exists"


class ConfigurationError(TwoDError):
    """A config file (safety limits, lights, scoring, cameras) is missing or invalid."""

    code = "config_invalid"
    exit_code = 1


class CameraDisconnectedError(TwoDError):
    """The camera stopped responding (cable, power or network)."""

    code = "camera_disconnected"
    exit_code = 3


class DeviceError(TwoDError):
    """A vendor SDK call failed (the message carries the SDK result code)."""

    code = "device_error"
    exit_code = 1
