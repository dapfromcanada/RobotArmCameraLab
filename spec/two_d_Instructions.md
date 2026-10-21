---
# Instructions for camera_lab — core + two_d (2D cameras and lighting)

> This spec is self-contained. The coding agent may have no prior context. Every
> rule here is explicit. Default to the strongest model tier (currently Claude
> Opus 5.5) unless a task is explicitly routed lower.
>
> Companion files: `two_d_Phase.md` (the gated build plan, same folder) and
> `apps/camera_lab/CONTEXT.md` (orientation + authoritative references).
> Written 2026-10-20.

## ⭐ Project name:
**camera_lab**, scope of this spec: the shared **core** (only the parts the 2D group needs) and the **two_d** group.
Python package: `camera_lab` (import name), distribution name `camera-lab`, console command `camera-lab`.
Location: `/mnt/workspaces/robotic_cell/apps/camera_lab/` (referred to below as `APP/`). The workspace root `/mnt/workspaces/robotic_cell/` is referred to as `WS/`.

## ⭐ Purpose:
A bench tool, used by both humans and an AI agent (Claude), that sits **outside** the robot-arm system. It is used to check the cell's 2D cameras and lighting and to tune them. It runs a loop: capture → score the image → adjust camera and LED settings → capture again. The best settings are saved as named presets, so the robot-arm system gets the best possible images.

## ⭐ Problem it solves:
Image quality for the robot cell depends mostly on exposure, gain, white balance and lighting. Today those are set by hand in vendor viewers (ArenaView, eBUS Player), one camera at a time, with no record of what was tried or why. This tool solves four problems:
1. One interface for every 2D camera, whatever the brand.
2. Objective, repeatable image scores that use no AI.
3. A tuning loop that either Claude (through MCP tools) or a classic non-AI algorithm can drive, with hard limits neither can override on lights, rates/budgets and protected camera features (see *Limits policy*).
4. A saved record of every capture, setting and score, plus presets the robot stack can load.

## ⭐ MVP definition:
1. **Discover** connected 2D cameras: real Lucid cameras through the Arena SDK, real JAI cameras through the eBUS SDK, and a built-in **simulated camera**. For each one, report vendor, model, serial, firmware, interface, IP and driver.
2. **Read and set camera settings**: exposure time, gain, gamma, white balance (per-channel ratio/gain), pixel format, region of interest (ROI), frame rate and trigger mode (free-run / software). Each value is checked against the camera's own allowed range before it is applied (camera_lab no longer narrows camera exposure/gain/frame rate; see *Limits policy*). Each change is read back and the actual value reported.
3. **Capture** one or more frames. Return them as numpy arrays with the settings actually used, the light levels, and a UTC timestamp.
4. **Score** each image deterministically, with no AI:
   - brightness histogram, mean level and percentiles
   - percent of clipped (saturated) pixels and percent of black pixels
   - sharpness (variance of Laplacian)
   - noise estimate
   - a documented composite quality score
5. **Light control** through core, using a `LightController` interface with a **simulated light** for now. The real LED hardware will be built later and plugs into the same interface. The simulated light affects the simulated camera's image brightness, so the whole loop can be tested without hardware.
6. **Tuning**, in two drivers that share one step-by-step run API:
   - (a) **agent-driven**: Claude calls MCP tools step by step.
   - (b) **classic baseline**: a built-in, non-AI auto-exposure/gain (+ grey-world white balance) search. It gives a testable reference to compare Claude against.
   Both are bounded by a step limit enforced in core.
7. **Save** every capture (TIFF + JSON sidecar), the run log and the scores under `WS/data/raw_recordings/camera_lab/two_d/<run_id>/`. Save the best settings + light levels as a named **preset** per camera, in YAML.
8. **Three thin front ends** over one service layer:
   - a **CLI**
   - an **MCP server** (stdio) for Claude
   - a **PySide6 desktop GUI** (dark mode): camera list, live view, setting sliders, light sliders, score panel, save-preset button, run browser.
   A **Python library API** is the service layer itself, importable by other apps. It includes a non-recording `TwoDService.grab()` for apps that need frames without camera_lab run records (see Integrations → "Non-recording grab").

## ⭐ Success criteria:
All of these must be demonstrable at the final gate:
1. `camera-lab two-d discover` lists the Lucid TRI054S-C (serial 233100505) and JAI AP-3200T-PGE (serial U330439) within 5 s. With `--sim` it also lists `SIM-0001`.
2. A camera request outside the camera's range, or a light request outside the light limits, is **rejected or clamped exactly as documented**, with a message naming the limit. Protected camera features are never written without the human's confirmation. The MCP server exposes **no** tool that can change the limits. There are tests for all three. *(Revised by Derek 2026-10-21, see Limits policy.)*
3. A single full-resolution capture + save completes in < 2 s on each real camera. 10 consecutive captures produce 10 frames with no missing or incomplete buffers.
4. Scoring is deterministic: the same image file gives bit-identical score JSON on repeated runs.
5. The classic baseline, on the simulated camera, converges in ≤ 12 steps to mean level within ±5 % of target with clipped ≤ 0.5 %, from 5 different starting exposures (tested). On each real camera, with a static scene, it converges within 15 steps (manual hardware test, logged).
6. Claude, connected through `.mcp.json`, completes an agent-driven tuning run end to end. The result is a run folder with ≥ 3 captures, `run.json`, `steps.jsonl`, and a saved preset.
7. In the GUI, live view runs at ≥ 5 fps (preview downscaled) on the TRI054S-C (lowered from 10 fps by Derek on 2026-10-21: image processing takes longer, and 5–6 fps is enough). The UI thread is never blocked > 200 ms (camera work runs in worker threads).
8. `pytest` (without hardware) passes. Line coverage is ≥ 80 % for `core/` and for `two_d/` excluding `two_d/drivers/arena.py`, `two_d/drivers/ebus.py` and `two_d/gui/`. `pytest -m hw` passes with both cameras connected.
9. `ruff check`, `ruff format --check`, `mypy src` and `pip-audit` report no errors and no high/critical findings.

## ⭐ Explicit non-goals / out of scope:
- **Calibration of any kind**: intrinsics, extrinsics, hand-eye, calibration board design or generation, and board detection. These will be a **separate app** later (several board types and sizes will be needed). This module only **reads** `WS/calibration/` and warns when a setting change may invalidate an existing calibration. *Exception (Derek, 2026-10-21, Phase 10):* camera_lab generates and detects its own **image-quality test charts** (step wedge, resolution, grey patch, tag cards), identified by AprilTag 36h11 ids 100–199. These are not calibration boards.
- 3D / depth data (`three_d`), laser profilers (`laser`), grasp planning, robot control.
- ROS / ROS 2. Do not import `rclpy`; ROS runs on system Python 3.14 and cannot be used from this env. A ROS wrapper may come later.
- Real LED hardware drivers. The hardware is not built yet. Ship only the interface + simulated light (see deferred features).
- BitFlow CoaXPress, RealSense colour and Helios2+ colour stream drivers. These are deferred, but the driver interface must allow them.
- Learned / per-object scoring thresholds. Thresholds are configurable YAML values for now.
- Image-processing utilities beyond what scoring needs. A plugin registry is provided so they "can be built as needed".
- A network-exposed API, multi-user access or authentication. The tool is local-only.
- Calling the Anthropic API from inside the app. Claude drives the app through MCP, not the other way round.

## ⭐ Primary user / persona:
- **Derek (technical owner/operator).** Uses the GUI and CLI at the bench to check cameras, lighting and settings, and reviews what the agent did.
- **Claude (AI agent)** in Claude Code. Uses the MCP tools to run tuning loops on its own, within safety limits it cannot change.
Both users get the **same operations with the same limits**. There is no admin/user split.

## ⭐ Top user stories:
1. *As an operator, I can list every connected 2D camera and see its model, serial, firmware and IP.*
   Accept: `discover` shows both real cameras, and the GUI camera list shows the same.
2. *As an operator, I can open a camera in the GUI, see a live image, move exposure/gain/white-balance/light sliders, and see scores update.*
   Accept: slider ranges equal the effective allowed range. Scores refresh on every preview frame (scoring the preview is fine; full-resolution scoring runs on "Capture").
3. *As Claude, I can start a tuning run, capture, see a preview image and scores, change settings and lights, repeat, then finish by choosing the best capture and saving it as a preset.*
   Accept: every step is in `steps.jsonl`. The step limit stops the run, and out-of-limit requests return structured rejections, never exceptions.
4. *As an operator, I can run the classic auto-tune from the CLI or GUI and compare its result to Claude's on the same scene.*
   Accept: both runs are saved in the same format. The run browser shows both, with scores side by side.
5. *As the robot stack (future consumer), I can load a named preset YAML and apply it to a camera.*
   Accept: `camera-lab two-d preset apply <camera> <name>` reproduces the saved settings (read back ±1 camera increment).

## ⭐ App type:
A **Python library + CLI + local MCP server (stdio) + desktop GUI (PySide6)**. One package, one service layer, three thin front ends.

## ⭐ Platform standards to follow:
Read these bundled files **before writing any code** in the matching phase. They are in `APP/spec/standards/`:
- `cli-api-standards.md`: for the service layer, CLI and MCP server. The MCP server counts as an "API" whose transport is stdio. Its HTTP-only parts (versioned URLs, pagination headers, rate-limiting public endpoints) do not apply. The validation, error-shape and logging rules do apply.
- `security-standards.md`: for everything, especially MCP input validation, file paths and safety limits.
- `gui-coding-standards.md`: for every file under `two_d/gui/`.

**GUI specifics:**
- **Layout approach: pure code.** No Qt Designer `.ui` files.
- **Always dark mode**, using the palette from `gui-coding-standards.md` §9 in `two_d/gui/styles.py`.
- Every `QWidget` subclass uses the 5-method `__init__` pattern.
- Every widget has a named instance variable.

**Dynamically generated widgets** must use factory + populate + clear (one factory per type):
| Widget | Factory method | Generated from |
|---|---|---|
| Camera list row | `_create_camera_row_widget(info: CameraInfo)` | `discover()` result |
| Setting control row (label + slider + spinbox + unit) | `_create_setting_row_widget(spec: SettingSpec)` | effective ranges of the open camera |
| Light channel row (name + slider + % value) | `_create_light_row_widget(channel: LightChannel)` | `LightController.channels()` |
| Score metric row (name + value + pass/fail marker) | `_create_score_row_widget(metric: MetricResult)` | registered metrics |
| Run browser row (thumbnail + run id + method + best score) | `_create_run_row_widget(summary: RunSummary)` | run folders on disk |
| Capture thumbnail (inside a run) | `_create_capture_thumb_widget(capture: CaptureRecord)` | run's captures |
| Plugin output row (processor name + result) | `_create_processor_row_widget(result: ProcessorResult)` | registered processors |

## ⭐ Key screens / endpoints / commands:

### Camera identifier
Everywhere, a camera is identified by its `camera_id`, which is the **serial number** string, e.g. `"233100505"`, `"U330439"` or `"SIM-0001"`. Optional human aliases come from `APP/config/cameras.yaml` (e.g. `triton: "233100505"`) and are resolved to serials at the boundary.

### CLI (`camera-lab`, built with Typer)
| Command | Does |
|---|---|
| `camera-lab two-d discover [--sim] [--json]` | List cameras |
| `camera-lab two-d settings get <camera> [--json]` | Current settings + effective allowed ranges |
| `camera-lab two-d settings set <camera> --exposure-us N --gain-db N --gamma N --wb R,G,B --pixel-format F --roi X,Y,W,H --fps N --trigger off\|software` | Apply validated changes. Prints applied / clamped / rejected per field |
| `camera-lab two-d capture <camera> [--count N] [--run RUN_ID] [--note TEXT]` | Capture + score + save. A run is created when `--run` is omitted |
| `camera-lab two-d score <image.tiff> [--json]` | Score an existing TIFF |
| `camera-lab two-d tune <camera> --method classic [--target-mean 0.45] [--max-steps 15] [--wb]` | Run the classic baseline |
| `camera-lab two-d preset list [<camera>]` / `preset save <camera> <name> --from-capture <run_id>/<capture_no>` / `preset apply <camera> <name>` / `preset show <camera> <name>` | Presets |
| `camera-lab two-d runs list [--camera C] [--json]` / `runs show <run_id>` | Browse runs |
| `camera-lab light list` / `light set <channel>=<pct> [...]` / `light off` | Lights |
| `camera-lab limits show` | Print effective safety limits (read-only) |
| `camera-lab mcp` | Start the MCP server on stdio |
| `camera-lab gui [--sim]` | Start the desktop GUI |

Global flags: `--version`, `--quiet`, `--log-level`, `--sim` (adds the simulated camera + simulated light).

**Exit codes:**
| Code | Meaning |
|---|---|
| 0 | OK |
| 1 | Unexpected error |
| 2 | Usage error |
| 3 | Camera not found / camera busy (locked by another process) |
| 4 | Rejected by safety limits or camera range |
| 5 | Vendor SDK not installed / failed to load |
| 6 | Capture timeout / incomplete buffer |

### MCP tools (server name `camera-lab`, stdio transport)
Every tool:
- validates its input with pydantic
- returns structured JSON
- returns failures as `{"ok": false, "error": {"code": "...", "message": "..."}}`, never as an unhandled exception
- has documented argument bounds

| Tool | Arguments (bounds) | Returns |
|---|---|---|
| `list_cameras` | `include_sim: bool = false` | list of `CameraInfo` |
| `get_camera_settings` | `camera_id` | current `CameraSettings` + effective ranges + which settings affect calibration |
| `set_camera_settings` | `camera_id`, `changes: CameraSettingsPatch` | per-field `applied` / `clamped` / `rejected` with reason, read-back values, warnings |
| `capture` | `camera_id`, `run_id: str \| None`, `count: int 1..5`, `note: str ≤ 500 chars` | `capture_ids`, scores per frame, and an **MCP image** preview of the last frame (JPEG, long edge ≤ 1024 px, quality 85) |
| `score_capture` | `capture_id` (`<run_id>/<NNNN>`) | scores (recomputed from disk) |
| `list_lights` | none | channels with current %, max % |
| `set_lights` | `levels: dict[channel_id, float 0..100]` | applied / clamped per channel |
| `lights_off` | none | ok |
| `start_tuning_run` | `camera_id`, `goal: str ≤ 500`, `max_steps: int 1..limit` | `run_id`, step budget, current settings, effective ranges, scoring targets |
| `finish_tuning_run` | `run_id`, `best_capture_id`, `preset_name: str \| None` (slug), `summary: str ≤ 2000` | final record; preset path if saved |
| `run_classic_tune` | `camera_id`, `target_mean: 0.1..0.9`, `max_steps`, `white_balance: bool` | run_id + result |
| `list_presets` / `get_preset` / `apply_preset` | `camera_id`, `name` | preset data / apply result |
| `get_safety_limits` | none | effective limits (read-only) |

The server's `instructions` string must explain the tuning workflow:
1. `start_tuning_run`
2. loop: `capture` → read scores + image → `set_camera_settings` / `set_lights`
3. `finish_tuning_run`

It must also say that limits are fixed and rejections are expected, normal feedback.

### GUI (`camera-lab gui`)
One main window, dark mode:
- **Left:** camera list (+ "Rescan" button).
- **Centre:** live view (`QLabel`-based, preview downscaled, optional clipped-pixel overlay toggle) and a histogram (painted with `QPainter`).
- **Right, as tabs:**
  - **Camera**: setting rows generated from effective ranges.
  - **Light**: channel rows.
  - **Scores**: metric rows + composite score.
  - **Runs**: run browser, capture thumbnails and a side-by-side compare of 2 captures.
- **Toolbar:** Start/Stop live, Capture, Run classic tune, Save preset…, Apply preset…, All lights off.
- **Status bar:** camera state, fps, lock holder, and the last warning, e.g. "ROI change may invalidate calibration".

## ⭐ Roles / permissions:
There are no user accounts or roles. The human and the agent have the same rights. The **only privilege boundary** is the limits file (lights, rates and budgets, protected camera features; see *Limits policy*):
- It is loaded at start-up from `APP/config/safety_limits.yaml`.
- They are **read-only at runtime** and cannot be changed through any CLI flag, MCP tool or GUI control.
- Changing them requires a human to edit the YAML file and restart the process.

## ⭐ Data to store + key rules:

### Run records
Stored under `WS/data/raw_recordings/camera_lab/two_d/<run_id>/` (git-ignored; backed up by `WS/scripts/backup.sh`).
- `run_id` = `YYYYMMDD-HHMMSS_<camera_serial>_<slug>`. The slug is `[a-z0-9-]{1,40}` derived from the note/goal, or `run`.
- `run.json` contains:
  - `schema_version: 1`, `run_id`
  - camera `CameraInfo`
  - `method` (`agent` | `classic` | `manual`), `operator` (`human` | `agent`), `goal`
  - `started_at` / `finished_at` (UTC ISO-8601)
  - `max_steps`, `steps_used`, `best_capture_id`, `preset_saved` (path or null), `summary`
  - `software`: camera_lab version, SDK name + version, firmware
  - `limits_hash` (sha256 of the safety-limits file)
- `steps.jsonl` holds one line per action: timestamp, actor, tool/command, arguments, result, and rejection reasons.
- `captures/NNNN.tiff` holds the raw frame **as delivered by the camera**:
  - Bayer stays Bayer
  - 10/12-bit packed data is unpacked to `uint16`, holding the camera's native values (not rescaled)
  - RGB stays RGB
  Write it with `tifffile` (lossless, no compression or zlib), and put the sidecar JSON text in the TIFF `ImageDescription` too.
- `captures/NNNN.json` contains:
  - `capture_id`, `timestamp_utc`
  - settings **as read back** from the camera, light levels
  - pixel format, bit depth, width/height, frame id
  - the scores (all metrics + composite), `preview` path
- `previews/NNNN.jpg` is a downscaled 8-bit RGB preview (long edge ≤ 1024 px) for the GUI/MCP.

### Presets (git-tracked, consumed by the robot stack)
Path: `WS/config/camera_presets/two_d/<camera_serial>/<name>.yaml`. `name` is a slug `[a-z0-9_-]{1,40}`.

Contents:
- `schema_version: 1`, `name`, `camera` (vendor/model/serial/firmware)
- `settings` (full `CameraSettings`), `lights` (channel→%)
- `tags` (object, lighting setup, free text list)
- `source_capture_id`, `scores_at_save`, `created_at`, `created_by` (`human` | `agent`)

If a preset already exists, it is never overwritten unless `overwrite=true` (CLI `--force`); the old file is then renamed `<name>.yaml.bak-<timestamp>`.

### Config (git-tracked, in `APP/config/`)
- `safety_limits.yaml`
- `scoring.yaml` (targets/thresholds/weights)
- `lights.yaml` (which light driver + channels)
- `cameras.yaml` (aliases, per-camera network/stream overrides)

### Key rules
- No PII and no secrets are stored.
- Never write anything under `WS/calibration/`.
- Never write outside `WS/data/raw_recordings/camera_lab/` and `WS/config/camera_presets/` at runtime.
- Every path built from user/agent input goes through a single `safe_join(base, *parts)` helper that rejects `..`, absolute paths and characters outside the slug set.
- No retention limit. Runs are kept until a human deletes them, and the app has **no delete function** for runs.

## ⭐ Security & data handling:

**Threat model.** We are protecting:
1. **physical safety and hardware**: LED brightness/heat/eye safety, and not hammering the cameras with settings writes
2. integrity of presets the robot will use
3. the filesystem outside the app's data folders

Hostile or unreliable input can come from:
- MCP tool arguments (an AI agent can send anything)
- CLI args and GUI values
- values and metadata returned by cameras
- YAML files edited by hand

Trust boundaries are every MCP call, every CLI invocation, every YAML load and every SDK return value.

**Controls:**
- **Safety limits are enforced in `core/safety.py` only**, and every code path (CLI, MCP, GUI, classic tuner) goes through it. There are no bypass flags. The limits file is validated by a pydantic model at start-up. If the file is invalid or missing, the app **refuses to start** (fail closed), except in `--sim`, where the built-in simulation defaults apply.
- **Limits policy (Derek, 2026-10-21).** Camera exposure, gain, frame rate and similar settings cannot damage a camera anywhere in the camera's own range, so camera_lab does **not** narrow them. The 18 dB / 200 ms caps were guardrails, and they blocked valid settings (e.g. restoring the TRI054S-C's own HDR channel gains of 19.8 / 29.4 dB).
  - **Camera numeric settings:** effective range = the camera-reported range. Out-of-range requests are rejected naming the camera range.
  - **Working range (guidance, not a limit):** each camera profile may give a preferred range per setting (e.g. exposure ≤ 50 ms, gain ≤ 12 dB). The classic tuner and the setup procedure steer inside it, and the agent is told it in tool descriptions and results. It never blocks a human, the agent or a restore.
  - **Hard limits that stay:** lights (`max_pct`, step size, change rate: heat and eye safety); rates and budgets (settings writes per second, tuning steps, captures per run, MCP capture count). These stop runaway loops.
  - **Protected camera features:** listed in `safety_limits.yaml` (`protected_features`, wildcards allowed). camera_lab never writes them through settings tools, and a restore skips them unless their value is unchanged. Writing them needs an explicit, confirmed human command. Defaults: network/IP configuration (`GevPersistent*`, `GevCurrentIPConfiguration*`), device reset and firmware/file access (`DeviceReset`, `DeviceFactoryReset`, `FileAccess*`, `FirmwareUpdate*`), and user-set save/default (`UserSetSave`, `UserSetDefault*`, covered by the confirmed save in the setup procedure). Node names are verified per camera, never guessed.
- **Effective range** (camera numeric settings) = the camera-reported range. If the camera reports no range, the setting is not adjustable, and the response says so.
- **Out-of-range policy**:
  - For `set_camera_settings` / `set_lights`, a value outside the effective range is **rejected** (not clamped), with a reason such as `"gain_db 45.0 > camera max 42.0"` or `"red 90 % > limit 80 % (safety_limits.yaml: lights.default.max_pct)"`.
  - The exception is the classic tuner, which clamps internally and logs the clamp.
  - Values not on the camera's increment are rounded to the nearest valid increment, and the response reports them as `clamped`.
- **Rate limits**, configurable in `safety_limits.yaml`:
  - ≤ 5 settings writes per second per camera
  - ≤ 2 light changes per second
  - an LED step of ≤ `max_step_pct` (default 25 %) per change. Larger requested changes are ramped in steps no faster than the rate limit.
  - a tuning-run step budget `max_steps` ≤ `limits.tuning.max_steps_hard` (default 40)
  - a captures-per-run cap (default 200)
  - an MCP `capture` count of ≤ 5 per call
- **Device lock.** One process controls a camera at a time. The lock is an `fcntl` lock file at `$XDG_RUNTIME_DIR/camera_lab/<serial>.lock` (fallback `/tmp/camera_lab-<uid>/`) and contains the PID + front-end name. A second opener gets exit code 3 / error `camera_busy` naming the holder. Also, never open the same camera from two SDKs: Lucid goes only through Arena, and JAI only through eBUS (see Integrations).
- **Lights off on exit**: on normal exit, SIGINT/SIGTERM, and unhandled exceptions in the light controller owner. The simulated light logs this; real drivers must implement it.
- **Network exposure: local only.** The MCP server uses **stdio only**. Do not enable SSE or streamable-HTTP transports. There is no HTTP server anywhere.
- **No secrets.** Nothing in this app needs a credential. Still create `APP/.env.example`, documenting `CAMERA_LAB_LOG_LEVEL`, `CAMERA_LAB_DATA_ROOT` and `CAMERA_LAB_WS_ROOT` (optional overrides for tests), and make sure `.env` is git-ignored.
- **Input validation.** All MCP/CLI inputs go through pydantic models with explicit bounds. All string inputs used in paths are slug-validated. YAML is loaded only with `yaml.safe_load`.
- **Logging.** structlog goes to **stderr** and also to the run's `steps.jsonl` (JSON). **Stdout is reserved for MCP stdio and for CLI result output**; any stray `print` breaks MCP. Stack traces never go into MCP responses (log them instead and return an error code + short message).
- **Supply chain.** Vendor wheels come only from `WS/SDK/` (local, already vetted). Run `pip-audit` at the hardening gate.
- **Security-sensitive work:**
  - `core/safety.py`
  - `core/paths.py` (`safe_join`)
  - the MCP server
  - lock handling
  - YAML loaders
  All of these go to the strongest tier with a human review step.

Compliance: none.

## ⭐ Integrations:

### Connected cameras (verified 2026-10-20)
| Camera | Serial | IP | Host NIC | Firmware | SDK used by this app |
|---|---|---|---|---|---|
| Lucid **Triton TRI054S-C** (colour, Bayer) | 233100505 | 192.168.20.229 | `enp9s0f1` 192.168.20.1/24 | 1.138.0.0 | **Arena SDK** via `arena_api` 2.10.0 |
| JAI **Apex AP-3200T-PGE** (3-CMOS prism RGB, GigE PoE) | U330439 | 192.168.10.44 | `enp9s0f0` 192.168.10.1/24, MTU 9000 | 0.1.0.7 | **eBUS SDK** 7.0.1 via `ebus_base` / `ebus_receive` |

Both SDKs can see both cameras (GigE Vision). Drivers are chosen by **vendor name**: `"Lucid Vision Labs"` → Arena and `"JAI Corporation"` → eBUS. The Arena driver must skip non-Lucid devices, and the eBUS driver must skip non-JAI devices. This mapping lives in one table in `two_d/drivers/__init__.py`.

### Driver interface (`two_d/drivers/base.py`)
An abstract class `Camera2DDriver` with these methods:
- `discover() -> list[CameraInfo]`
- `open(camera_id) -> Camera2D`

And an abstract `Camera2D` with:
- `info`
- `capabilities() -> dict[str, SettingSpec]` (min/max/increment/enum options/unit, read live from the camera)
- `get_settings() -> CameraSettings`
- `apply(patch: CameraSettingsPatch) -> ApplyResult`
- `start_stream()` / `stop_stream()`
- `grab(timeout_ms) -> Frame`
- `close()`

Drivers know **nothing** about safety limits, scoring or files. `two_d/service.py` composes them with core.

### Non-recording grab (`TwoDService.grab`)
`TwoDService.grab(camera_id, timeout_ms=2000) -> Frame` is the **public** way to get a frame without recording anything. The GUI live loop uses it, and so does the separate calibration app (`WS/apps/calibration_lab/`) for live capture.
- The camera must already be open in this process (the caller holds the lock through `open()`); otherwise it raises `CameraNotOpenError`.
- It starts the stream if needed and returns a copied `Frame` with the settings as last read back.
- It writes **nothing**: no run folder, images, `steps.jsonl` line or scores. It uses no tuning-run step.
- A timeout or incomplete buffer raises the same error as `capture()` (exit code 6).
- `capture()` is built on `grab()` and then records and scores, so there is one grab path.

See `two_d_Phase.md` Phase 4 for the tasks and tests.

### Arena driver (`two_d/drivers/arena.py`)
- `from arena_api.system import system`.
- Disable all camera auto functions on open: `ExposureAuto=Off`, `GainAuto=Off`, `BalanceWhiteAuto=Off`. Nothing may auto-adjust behind the app.
- Set the stream nodes `StreamAutoNegotiatePacketSize=True` and `StreamPacketResendEnable=True`.
- Map settings to GenICam SFNC nodes:
  - `ExposureTime` (µs)
  - `Gain` (dB)
  - `Gamma` (+ `GammaEnable` if present)
  - `BalanceRatioSelector` / `BalanceRatio`
  - `PixelFormat`
  - `Width` / `Height` / `OffsetX` / `OffsetY`
  - `AcquisitionFrameRateEnable` / `AcquisitionFrameRate`
  - `TriggerMode` / `TriggerSource=Software` / `TriggerSoftware`
- **Verify every node name and its behaviour against `WS/Manuals/vision/lucid_triton/Lucid_Triton_TRI054S_fw1.138.0.0.toc.md` → `.rag.md`.** If a node is missing on the device, report the setting as unsupported rather than guessing.
- Always call `device.requeue_buffer(buffer)` after copying the data, and `system.destroy_device()` on close.
- Use `BufferFactory.copy` before requeue if the buffer is kept.

### eBUS driver (`two_d/drivers/ebus.py`)
- Wheels: `ebus_base`, `ebus_edge`, `ebus_receive` 7.0.1. Imports verified to work: `from ebus_base import PvSystem`.
- Follow the patterns in:
  - `WS/Manuals/vision/jai_ebus/eBUS_Python_API_Quick_Start_Guide_7.0.1.rag.md`
  - the official samples in `/opt/jai/ebus/Ubuntu-24.04-x86_64/share/samples/python_samples/`
  Use device connect → stream open → pipeline/buffers → acquisition start/stop.
- Camera feature names come **only** from `WS/Manuals/vision/jai_apex/JAI_Apex_AP-3200T-PGE_manual_v2.2.rag.md`:
  - `.toc.md` sections: Pixel Format L1455, Exposure Mode L1782–L2130, Gain Control L2131, White Balance L1234, Gamma L2284, LUT L2240, and the full **Setting List (Feature Properties) L3261**
  - The AP-3200T is a **3-sensor prism** camera: white balance is done with per-channel gain via a selector. Implement the WB mapping exactly as the manual's setting list describes.
- Disable auto gain / auto exposure / auto WB on open.
- Use packet size 9000 on `enp9s0f0` (jumbo frames already set on the host).

### Simulated camera (`two_d/drivers/simulated.py`)
- Deterministic (seeded) synthetic scene: a grey-scale step chart + colour patches + a sharp edge target. Mono8/Mono12/BayerRG8/RGB8.
- Pixel value = scene reflectance × exposure_us × 10^(gain_db/20) × light_factor, with Poisson + read noise (seeded), clipped at the bit depth.
- `light_factor` comes from the simulated light (sum of channel % × channel weight).
- An optional `defocus_sigma` lets sharpness tests work.
- Serial `SIM-0001`, vendor `"camera_lab"`, model `"Simulated2D"`.

### Lights (`core/lights/`)
- `LightController` ABC:
  - `channels() -> list[LightChannel]` (id, name, colour/wavelength text, max_pct)
  - `get_levels()`
  - `set_levels(dict[str, float])`
  - `all_off()`
  - `close()`
- `SimulatedLight` keeps levels in memory and is shared in-process with the simulated camera.
- Real driver(s) will be added when the LED hardware is built. `lights.yaml` selects the driver class by name through a registry, so adding hardware = one new module + config, with no changes elsewhere.

### Camera profiles and tuning strategies (added 2026-10-21, Phase 6b)
camera_lab is meant to grow into a **library of profiled cameras** that can be compared and chosen per situation (cost vs performance; rolling vs global shutter; HDR; prism). Each camera has a git-tracked profile `APP/config/camera_profiles/<serial>.yaml`:
- `characteristics`: shutter (`rolling` / `global`), colour (`bayer` / `prism` / `mono`), HDR capability, measured black-level pedestal, notes.
- `modes`: per operating mode (e.g. TRI054S-C `hdr` [default] and `linear`; AP-3200T-PGE `prism`) the tuning `strategy`, the `settings` applied on entering the mode, the `knobs` the tuner and Claude may change, and `scoring` overrides.
- Vendor/mode-specific settings (HDR channels, per-sensor exposure) are `advanced` settings: described by live `SettingSpec`s and validated by the same safety flow as core settings (limits by unit, optional per-name overrides in `safety_limits.yaml`).
- Strategies (`linear`, `prism`, `hdr`) are registry plug-ins. Opening a camera never changes its mode; tuning or `mode set` does.
See `two_d_Phase.md` Phase 6b.

### Calibration (read-only)
- When a setting in `{roi, binning, decimation, reverse_x, reverse_y, pixel size/format changes that alter resolution, focus, zoom}` changes on a camera, check whether `WS/calibration/intrinsics/<serial>*` or `WS/calibration/extrinsics/<serial>*` exists.
- If it does, add the warning `"this change may invalidate calibration in <path>"` to the response, the log and the GUI status bar.
- Do not parse calibration files. Their format belongs to the future calibration app.

### MCP client (Claude Code)
Create `APP/.mcp.json`:
```json
{ "mcpServers": { "camera-lab": { "command": "/mnt/workspaces/robotic_cell/environments/camera_lab/bin/camera-lab", "args": ["mcp"] } } }
```
Add `--sim` to `args` for a hardware-free session.

## ⭐ Target OS and hardware:
- Ubuntu **26.04.1 LTS** desktop, x86_64, glibc 2.43.
- Threadripper 1920X, NVIDIA RTX A4000. No GPU is needed by this module, so do not add CUDA dependencies.
- Camera NICs: `enp9s0f0` 192.168.10.1/24 (MTU 9000) and `enp9s0f1` 192.168.20.1/24. `net.core.rmem_max` = 32 MB.
- The eBUS 7.0.1 packages are built for Ubuntu 24.04 but were verified on 26.04 (import + device enumeration both work).
- The Arena SDK 1.0.10.11 is in `WS/SDK/lucid_arena/ArenaSDK_Linux_x64`.

## ⭐ Version control and environment:
- Git repo at `WS/` (branch `main`). `SDK/`, `Manuals/`, `data/` and `environments/` are git-ignored. **Do not commit or push** unless Derek asks.
- **uv 0.13.4**. The environment is the existing uv venv `WS/environments/camera_lab` (CPython 3.12.15). Always export `UV_LINK_MODE=copy`. Install with:
  `uv pip install --python WS/environments/camera_lab/bin/python ...`
  Do **not** create another venv and do **not** use `uv sync`/`uv add`; the workspace convention is uv venv + `uv pip`.
- Project metadata goes in `APP/pyproject.toml` (src layout, hatchling build backend, `requires-python = "==3.12.*"`, console script `camera-lab = camera_lab.cli:app`).
- Pinned versions go in `APP/requirements.lock`, generated with:
  `uv pip compile pyproject.toml --extra dev --python-version 3.12 --python-platform x86_64-manylinux_2_39 -o requirements.lock`
- **Vendor wheels are not listed in `pyproject.toml`.** `APP/scripts/setup_env.sh` installs them from `WS/SDK/` paths. Vendor imports are **lazy** (inside the driver modules only), so the package imports and tests run with no SDK installed.
- Every environment change gets a short entry in `WS/docs/INSTALL_LOG.md` under section "6e. apps/ folder and camera_lab" (workspace convention: "update it every time something changes").

## ⭐ Preferred tech stack:
Verified on 2026-10-20 by resolving with `uv pip compile` for Python 3.12 / manylinux_2_39. `pip-audit` reported **no known vulnerabilities**.

| Tool / library | Version | Role | Notes / gotchas |
|---|---|---|---|
| Python | 3.12.15 | runtime | **Required**: eBUS wheels are `==3.12.*` |
| uv | 0.13.4 | env + installs | `UV_LINK_MODE=copy` |
| arena_api | **2.10.0** | Lucid driver | Wheel: `WS/SDK/lucid_arena/ArenaSDK_Linux_x64/OutputDirectory/Linux/x64Release/ArenaPy/arena_api-2.10.0-py3-none-any.whl`. API docs on disk are for 2.9.5 (closest available) |
| ebus_base / ebus_edge / ebus_receive | **7.0.1** (build 7536) | JAI driver | Wheels in `WS/SDK/jai_ebus/`. **Pin `numpy<2`.** The runtime is installed in `/opt/jai/ebus/Ubuntu-24.04-x86_64`; never move it |
| numpy | **1.26.4** | arrays | Forced by eBUS. Do not upgrade to 2.x |
| opencv-python-headless | **4.11.0.86** | Laplacian, demosaic, resize, JPEG | The last line compatible with numpy 1.x (OpenCV 5.0 needs numpy 2). Use **headless**: the GUI version bundles Qt and conflicts with PySide6 |
| tifffile | **2026.3.3** | lossless TIFF I/O | The last release compatible with numpy 1.x |
| pydantic | 2.14.1 | models, validation, JSON | — |
| PyYAML | 6.0.3 | config/presets | `safe_load` only |
| mcp (Python SDK) | **2.3.0** | MCP server | ⚠️ **v2 API.** Use `from mcp.server.mcpserver import MCPServer, Image`; `server.tool()` decorator; `server.run(transport="stdio")`. **`FastMCP` / `mcp.server.fastmcp` no longer exist in v2.** Agents trained on v1 examples will get this wrong. Check the official v2 migration guide if unsure |
| typer | 0.27.3 | CLI | — |
| PySide6-Essentials | **6.12.0** | GUI | Install **`PySide6-Essentials`** only (QtCore/QtGui/QtWidgets), not the `PySide6` meta-package (which pulls ~300 MB WebEngine/Addons). Needs glibc ≥ 2.39; host has 2.43 |
| structlog | 26.1.0 | logging | stderr + JSON |
| pytest | 9.1.1 | tests | — |
| pytest-qt | 4.5.0 | GUI tests | Set `QT_QPA_PLATFORM=offscreen` for headless runs |
| pytest-cov | 7.1.0 | coverage | — |
| ruff | 0.17.1 | lint + format | — |
| mypy | 2.4.0 | type check | `strict = true` for `core/`; vendor modules get `ignore_missing_imports` |
| pip-audit | 2.10.1 | CVE audit | — |

Not used, on purpose:
- **scikit-image** (needs numpy 2)
- `opencv-python` (non-headless)
- the full `PySide6` meta-package
- Aravis (installed system-wide, but it is a different GenICam stack; do not mix it in)
- `pyrealsense2` (deferred)

## ⭐ Testing approach:
- **pytest** throughout. Tests live in `APP/tests/{core,two_d}/`, mirroring the source tree.
- **No hardware needed by default.** Everything runs against `SimulatedCamera` + `SimulatedLight`.
- **Hardware tests** are marked `@pytest.mark.hw`, skipped unless `-m hw` is passed, and also skipped automatically if the SDK or camera is absent. They must be run by a human at the bench and recorded in the phase report.
- Required test families:
  - **Safety:** every limit; reject vs clamp; rate limits; there is no MCP path to change limits (introspect the registered tools); invalid limits file → refuses to start.
  - **Paths:** `safe_join` traversal attempts; slug validation.
  - **Scoring:**
    - golden images (synthetic arrays with known mean/clipping/black %)
    - determinism (same input → identical JSON)
    - sharpness monotonic with blur sigma
    - noise estimate within ±15 % of the injected sigma
  - **Classic tuner** convergence from 5 starts (success criterion 5).
  - **Records:** round-trip TIFF bit-exact for Mono8/Mono12-in-uint16/BayerRG8/RGB8; sidecar schema; run.json schema; preset overwrite → `.bak`.
  - **MCP:** start the server in-process with the SDK's test client/session helpers, call each tool with valid, boundary and invalid arguments, and assert the error shape. Assert nothing is printed to stdout outside the protocol.
  - **CLI:** Typer `CliRunner` for each command, plus exit codes.
  - **GUI:** pytest-qt smoke tests: the window opens with `--sim`, slider moves call the service, the factory/populate/clear functions leave no orphan widgets, and the dark palette is applied.
  - **Lock:** a second process opening the same sim camera gets `camera_busy`.
- Coverage targets as in success criterion 8.

## ⭐ Preferences and tradeoffs:
- **Clarity and safety over cleverness.** Small modules, typed (`mypy`), with docstrings on public functions.
- **Minimal dependencies.** Don't add a library for something numpy/OpenCV already does.
- **Extensible by registry, not by editing core.** Scoring metrics (`two_d/scoring.py`), image processors (`two_d/processing.py`), light drivers (`core/lights/`) and camera drivers (`two_d/drivers/`) each register themselves through a decorator-based registry. Adding one is a new module + one config entry.
- **One service layer.** `two_d/service.py` (`TwoDService`) is the library API. CLI, MCP and GUI contain **no** business logic.
- **Deterministic scoring, AI only in the agent.** The app never calls an LLM.
- **Defer, don't design out:** BitFlow CXP (JAI Spark SP-45001-CXP2A, datasheet in `Manuals/vision/jai_spark/`), RealSense D435i colour, Helios2+ colour stream, real LED drivers, per-object learned thresholds, sequencer modes, hardware triggering, camera characterisation and shoot-out commands, stereo pairs, three_d/laser groups. (HDR modes moved into Phase 6b on 2026-10-21.) The driver and light interfaces must accommodate them without changes.
- **Units:**
  - exposure in **µs** (`exposure_us`)
  - gain in **dB** (`gain_db`)
  - light levels in **% of channel max** (0–100)
  - image levels normalised **0–1** of the pixel format's full scale

## ⭐ Reference material:
See `APP/CONTEXT.md` for the full list, read protocol and what to ignore. Authoritative references (TOC-first protocol, **never read a `.rag.md` whole**):

| Reference | Covers |
|---|---|
| `WS/Manuals/vision/lucid_triton/Lucid_Triton_TRI054S_fw1.138.0.0.{toc,rag}.md` | Triton TRI054S features/nodes, **exact firmware match** |
| `WS/Manuals/vision/lucid_arena/arena_api_2.9.5_python_api.{toc,rag}.md` | arena_api Python API (2.9.5 docs; installed 2.10.0) |
| `WS/Manuals/vision/lucid_arena/ArenaSDK_python_examples_guide.{toc,rag}.md` + `WS/SDK/lucid_arena/arena_api-2.9.5/examples/*.py` | Arena usage patterns |
| `WS/Manuals/vision/jai_apex/JAI_Apex_AP-3200T-PGE_manual_v2.2.{toc,rag}.md` | JAI AP-3200T-PGE features (manual v2.2) |
| `WS/Manuals/vision/jai_ebus/eBUS_Python_API_Quick_Start_Guide_7.0.1.{toc,rag}.md` + `/opt/jai/ebus/Ubuntu-24.04-x86_64/share/samples/python_samples/` | eBUS Python API |
| `WS/Manuals/vision/jai_ebus/eBUS_SDK_7.x_Install_and_Release_Notes_JAI.{toc,rag}.md`, `eBUS_Linux_Quick_Start_Guide.{toc,rag}.md` | install / NIC tuning |
| `WS/docs/SoftwareList.md`, `WS/docs/HardwareList.md`, `WS/docs/INSTALL_LOG.md` | workspace setup facts |
| `APP/src/camera_lab/two_d/README.md` | original intent for this module |

---

## Rules for the coding agent

- **Read the bundled platform standards file(s) named above before writing any code** (`APP/spec/standards/`), and follow them exactly.
  - GUI desktop: always dark mode. Every QWidget subclass uses the 5-method `__init__` pattern (`_create_widgets`, `_create_layouts`, `_connect_signals`, `_apply_styles`, `_load_initial_state`). Every widget has a named instance variable. No business logic in `_create_*` methods. Dynamic widgets use factory + populate + clear.
  - CLI / MCP: follow the structure, error-handling and logging rules in `cli-api-standards.md`.
  - Security: follow `security-standards.md` for any data, network or user input.
- Ask if anything is unclear BEFORE starting. Never assume silently.
- Never delete or overwrite existing files without explicit confirmation. That includes anything in `WS/calibration/`, `WS/Manuals/`, `WS/SDK/` and existing presets.
- Write a phased implementation plan before any code (see `two_d_Phase.md`, and follow it).
- List required software and versions per phase.
- Between phases: run the full test suite, report results, and wait for "proceed". Do not advance if any test fails. Each phase must leave the codebase runnable. The proceed decision is made at the strongest tier (currently Opus 5.5), and is never delegated.
- Never work on two interdependent files simultaneously.
- If a dependency cannot be installed or versions conflict, STOP and report the exact error before continuing. **Never upgrade numpy to 2.x** to fix a conflict.
- If unsure whether a library is current or deprecated, web-search to verify before using it; version knowledge goes stale. This applies especially to the `mcp` v2 API.
- No AI-generated placeholder data in production logic. The simulated camera/light are real, deterministic, tested components, not placeholders.
- Keep secrets and API keys in a `.env` file (none are needed now). Never hard-code them. Validate and sanitize all external input.
- **Camera-specific rule:** never guess a GenICam node name. Look it up in the matching manual through its `.toc.md`, or read it live from the device node map. If it differs from this spec, the manual/device wins; report the discrepancy.
- **Hardware rule:** stop streaming before changing `PixelFormat`, ROI size, binning or decimation. Use `TLParamsLocked`-type behaviour as the SDK requires. Always release buffers, close devices and turn lights off in `finally` blocks.
- **Stdout rule:** nothing may write to stdout except CLI result output and the MCP protocol. Use the logger.

## Agent routing hints (capability-based, with named defaults)

Route by capability tier so cost is saved on trivial work. The strongest tier is the default and the supervisor. Model names change, so the tier is always paired with the current default model; this table still works if a name goes stale.

*Model names verified October 2026. If this spec is much older than that, confirm the names below are still current before relying on them.*

| Tier | Default model | Model string | Use for |
|------|---------------|--------------|---------|
| Strongest | Claude Opus 5.5 | `claude-opus-5-5` | Architecture, spec review, security review, complex/cross-file debugging, phase-gate decisions |
| Mid | Claude Sonnet 5.5 | `claude-sonnet-5-5` | Feature implementation, boilerplate, refactoring, test writing |
| Cheapest | Claude Haiku 5.5 | `claude-haiku-5-5` | Renames, formatting, trivial one-line edits |

- **When in doubt, use the strongest tier.** Lesser tiers take only genuinely trivial, easily verified work.
- **Escalate on doubt.** If a lesser tier produces anything questionable, or a task is harder than it looked, hand it to the strongest tier.
- **Security-sensitive work is strongest-tier only:** `core/safety.py`, `core/paths.py`, locks, YAML loaders and the MCP server. It also gets a human review step.
- **Vendor-driver work** (Arena, eBUS) goes to the strongest tier, because it involves hardware, manuals and buffer lifetimes.
- GUI layout generation and any security-sensitive change get a human review step before proceeding.

## First tasks when coding begins

1. Read `APP/CONTEXT.md` to orient: what each folder holds, which reference material is authoritative, what to ignore, and what's sample input vs. deliverable. Keep `CONTEXT.md` current as reference files are added or versions change.
2. Read the bundled standards files in `APP/spec/standards/` named in this spec.
3. Read and list all required software, libraries and extensions (Phase 1 table in `two_d_Phase.md`).
4. Web-search to verify the versions above are still current and mutually compatible. Re-run the `uv pip compile` check. The numpy<2 constraint must hold.
5. Confirm `APP/` and `WS/environments/camera_lab` exist (they do as of 2026-10-20). Create any missing folders.
6. Install all dependencies into `WS/environments/camera_lab` (Phase 1).
7. Create `APP/.env.example` and make sure `.env` is git-ignored (no real secrets exist).
8. Report setup complete with a version summary, then wait for "proceed" before writing any application code.
