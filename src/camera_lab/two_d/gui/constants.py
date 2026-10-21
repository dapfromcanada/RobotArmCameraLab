"""GUI-wide constants (lock owner, actor, rates and thresholds)."""

from __future__ import annotations

#: Lock owner and actor the GUI passes to the service (cli uses "cli", mcp "mcp"/"agent").
LOCK_OWNER = "gui"
ACTOR = "human"

#: Live view preview: long edge in pixels, maximum frame rate (spec: <= 15 fps).
PREVIEW_LONG_EDGE_PX = 640
MAX_LIVE_FPS = 15.0
LIVE_GRAB_TIMEOUT_MS = 500
LIVE_MAX_CONSECUTIVE_ERRORS = 5
FPS_WINDOW_FRAMES = 10

#: UI heartbeat (spec: no UI freeze > 200 ms).
HEARTBEAT_INTERVAL_MS = 10
STALL_LIMIT_MS = 200.0

#: Settings/lights edits are coalesced for this long before one apply is sent.
DEBOUNCE_MS = 150

#: Classic tune step budget used by the toolbar action.
TUNE_MAX_STEPS = 15

#: Histogram bins (matches the scoring histogram).
HISTOGRAM_BINS = 256
