# two_d — 2D camera module

Part of [camera_lab](../../../README.md). **Status: implemented (Phases 1–8, 2026-10-21).** Usage, CLI, MCP and GUI are in the app README; agent context and live camera facts are in `CONTEXT.md`.

## Purpose

Handle every camera in the cell that produces a normal 2D image (mono, colour, Bayer, prism RGB), and let a human or an AI agent tune the camera and the LED lighting:

capture → score the image → adjust camera and light settings → capture again → keep the best settings.

## Cameras

| Camera | Interface | Driver / SDK | Status |
|---|---|---|---|
| Lucid Triton TRI054S-C (HDR, rolling shutter) | GigE Vision | `drivers/arena.py`, Arena `arena_api` 2.10.0 | done |
| Lucid Triton TRI089S-M (mono, global shutter) | GigE Vision | `drivers/arena.py` | done |
| Lucid Triton TRI089S-C (colour) | GigE Vision | `drivers/arena.py` | next: needs a profile |
| JAI Apex AP-3200T-PGE (3-sensor prism) | GigE Vision | `drivers/ebus.py`, eBUS 7.0.1 | done |
| Simulated camera | — | `drivers/simulated.py` | done (tests, demos, `--sim`) |
| BitFlow Claxon CXP4 + CoaXPress cameras | CoaXPress | BitFlow SDK | later (SDK not installed) |
| Colour streams of Helios2+ Chroma / RealSense D435i | GigE / USB | Arena / librealsense2 | later (depth side is `three_d`) |

Each camera type has one driver behind `drivers/base.py`; the Arena driver only opens Lucid devices, and the JAI is only opened through eBUS.

## What is in the module

- `models.py`, `errors.py` — settings, patches, frames, scores, typed errors (with CLI exit codes).
- `drivers/` — simulated, Arena, eBUS; packed-format decoders in `unpack.py`.
- `scoring.py`, `processing.py` — metric and processor registries: mean level, histogram, clipped / black pixels, sharpness, noise, composite score with gates from `config/scoring.yaml`.
- `profiles.py`, `strategies/` — per-camera profiles (`config/camera_profiles/<serial>.yaml`) with modes and tuning strategies: `linear`, `prism`, `hdr`.
- `tuning.py` — the step API used by agents and the classic (non-AI) tuner.
- `calib_guard.py` — read-only warnings when a change may invalidate a calibration in `WS/calibration/`.
- `service.py` — `TwoDService`, the only API the CLI, MCP server and GUI use; every change goes through `core/safety.py`.
- `cli.py` — the `two-d` command group. `gui/` — the PySide6 desktop GUI.

## Rules

- **Safety limits live in `core`.** LED brightness, exposure and gain ranges and the rate of changes cannot be overridden by any front end or the agent.
- **Calibration is separate**: read, never written; ROI and similar changes produce a warning.
- **Free-run by default.** Settings that the camera locks while streaming are written only when they change; frame-rate changes that need it pause the stream briefly.
- After any change, grabbed frames reflect the new settings (Arena: device-timestamp check; eBUS: frame flush).
- No ROS. A ROS wrapper can come later.

## Not in scope

3D / depth data (`three_d`), laser profile sensors (`laser`), grasp planning, robot control.

## Answered questions

- *Which camera models first?* TRI054S-C, AP-3200T-PGE, then TRI089S-M; TRI089S-C next.
- *LED controller?* Not chosen yet. A simulated light stands in; real controllers plug into `core/lights/` behind the `LightController` interface.
- *Scoring thresholds?* Fixed values in `config/scoring.yaml`, overridable per profile mode (e.g. HDR uses a lower target mean). Target-finding scores (can the camera find the parts to pick) are planned as a later phase.
