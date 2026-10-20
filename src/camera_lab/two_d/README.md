# two_d — 2D camera module

Part of [camera_lab](../../../README.md). **Status: planned. Nothing here is implemented yet; this file describes the intended purpose and basic functions so the spec and code can follow it.**

## Purpose

Handle every camera in the cell that produces a normal 2D image (mono, colour, Bayer or raw), and let an AI agent tune the camera and the LED lighting on its own:

capture → score the image → adjust camera and light settings → capture again → keep the best settings.

Image quality here depends mostly on exposure, gain and lighting, so this is the group where the cell's LEDs matter most.

## Cameras in scope

| Camera | Interface | SDK / library | Notes |
|---|---|---|---|
| JAI cameras | GigE Vision | eBUS SDK (`ebus_*` Python wheels, Python 3.12) | SDK installed in `/opt/jai/ebus` |
| BitFlow Claxon CXP4 + CoaXPress cameras | CoaXPress | BitFlow SDK | SDK not installed; waiting on card power adapter |
| Lucid cameras (e.g. Triton) | GigE Vision | Arena SDK (`arena_api` 2.10.0) | SDK installed |
| Colour streams of Helios2+ Chroma and RealSense D435i | GigE / USB | Arena SDK / librealsense2 | Depth side belongs in `three_d` |

Each camera type gets its own small driver behind one shared interface, so the rest of the app does not care which brand it is talking to.

## Basic functions

1. **Discover** – list the 2D cameras that are connected (name, serial, interface, IP).
2. **Read / set settings** – exposure time, gain, gamma, white balance, pixel format, region of interest, frame rate, trigger mode. Each setting is checked against the camera's allowed range before it is applied.
3. **Capture** – grab one or more frames and return them as arrays with the settings that were used and a timestamp.
4. **Score** – measure the image without any AI so results are repeatable:
   - brightness histogram and mean level
   - percent of clipped (saturated) and black pixels
   - sharpness / focus measure
   - noise estimate
5. **Light control** – ask `core` to set LED brightness per channel before a capture (lighting hardware not chosen yet; a simulated light is used until then).
6. **Tuning loop** – the agent (Claude, through the MCP tools in `core`) looks at the image and the scores, changes settings, and repeats until the image is good or a step limit is reached.
7. **Save** – store each capture, its settings and its scores under `data/raw_recordings/camera_lab/two_d/<run>/`, and save the best settings as a named preset per camera (and per object or lighting setup).

## Rules

- **Safety limits live in `core`, not here.** Maximum LED brightness, allowed exposure and gain ranges and the rate of changes cannot be overridden by the AI.
- **Calibration is separate.** Intrinsics and extrinsics are stored in `calibration/` and are read, not written, by this module. Changing focus or zoom can invalidate a calibration, so the module should warn when a setting that affects it is changed.
- **Stop streaming before changing format-type settings** (pixel format, binning and similar) on cameras that require it.
- The module does not use ROS. A ROS wrapper can come later.

## Not in scope

3D / depth data (`three_d`), laser profile sensors (`laser`), grasp planning, robot control.

## Environment

`environments/camera_lab` (uv, Python 3.12). Vendor wheels are installed from the `SDK/` folder, as in `perception`.

## Open questions

- Which JAI and Lucid camera models will be used first?
- Which LED controller (USB, serial, DMX, PWM board) and what maximum current?
- Image scoring thresholds: fixed values, or learned per object?
