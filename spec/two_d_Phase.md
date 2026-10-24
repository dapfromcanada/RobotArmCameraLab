---
# Implementation Plan for camera_lab — core + two_d

> Gated phases. After each phase, run the full test suite, report the results, and
> wait for "proceed". Do not advance if any test fails. Each phase must leave the
> codebase in a runnable state. The proceed decision is made at the strongest
> model tier (currently Claude Opus 5.5).
>
> Read `two_d_Instructions.md` (full spec) and `../CONTEXT.md` first.
> Paths: `WS` = `/mnt/workspaces/robotic_cell`, `APP` = `WS/apps/camera_lab`,
> `PY` = `WS/environments/camera_lab/bin/python`. Always `export UV_LINK_MODE=copy`.
>
> **Gate procedure (every phase):**
> 1. `cd APP && QT_QPA_PLATFORM=offscreen PY -m pytest -q`
> 2. `PY -m ruff check src tests && PY -m ruff format --check src tests && PY -m mypy src`
> 3. For phases with hardware: Derek runs `PY -m pytest -m hw -q` at the bench.
> 4. Report: tests passed/failed, coverage, anything deviating from the spec,
>    manual/node-name discrepancies found, and the `INSTALL_LOG.md` entry if the
>    env changed.
> 5. Wait for "proceed".

## Target source layout (built up across phases)

```
APP/
├── pyproject.toml, requirements.lock, .env.example, .mcp.json, CONTEXT.md, README.md
├── config/          safety_limits.yaml, scoring.yaml, lights.yaml, cameras.yaml
├── scripts/         setup_env.sh
├── spec/            two_d_Instructions.md, two_d_Phase.md, standards/
├── src/camera_lab/
│   ├── __init__.py  (__version__)
│   ├── cli.py       (Typer root app; mounts two_d + light + limits + mcp + gui sub-apps)
│   ├── core/
│   │   ├── paths.py        WS/data/config roots, safe_join, slug validation
│   │   ├── logging.py      structlog setup (stderr; JSON sink per run)
│   │   ├── config.py       YAML loading (safe_load) → pydantic models
│   │   ├── safety.py       SafetyLimits model, validate/clamp, RateLimiter, StepBudget
│   │   ├── locks.py        per-camera fcntl lock
│   │   ├── registry.py     generic decorator registry (drivers, lights, metrics, processors)
│   │   ├── records.py      RunRecorder: run.json, steps.jsonl, captures, previews
│   │   ├── presets.py      Preset model, save/load/list, .bak on overwrite
│   │   ├── lights/         base.py (LightController ABC), simulated.py, __init__.py (registry)
│   │   └── mcp_server.py   MCPServer + tool definitions (thin, calls services)
│   └── two_d/
│       ├── models.py       CameraInfo, SettingSpec, CameraSettings, CameraSettingsPatch,
│       │                   ApplyResult, Frame, MetricResult, ScoreReport
│       ├── drivers/        base.py, simulated.py, arena.py, ebus.py, __init__.py (vendor→driver table)
│       ├── scoring.py      metric registry + built-in metrics + composite
│       ├── processing.py   processor registry (to_luma, demosaic, preview) — extend later
│       ├── tuning.py       TuningRun step API + ClassicTuner
│       ├── calib_guard.py  read-only calibration-invalidation warnings
│       ├── service.py      TwoDService — the library API used by CLI/MCP/GUI
│       ├── cli.py          `two-d` Typer sub-app
│       └── gui/            app.py, main_window.py, widgets/*.py, workers.py, styles.py
└── tests/core/, tests/two_d/, tests/conftest.py (fixtures: tmp data root, sim camera, sim light)
```

---

## Phase 1 — Environment Setup
**Complexity:** Simple
**Goal:** The env has every dependency at the pinned versions, the package skeleton installs in editable mode, `camera-lab --version` runs, and an empty test suite passes.

### Dependencies
| Tool / Library | Version | Purpose |
|----------------|---------|---------|
| uv | 0.13.4 | installs (`uv pip`) |
| Python | 3.12.15 | existing venv `WS/environments/camera_lab` |
| numpy | 1.26.4 | arrays (forced by eBUS `numpy<2`) |
| opencv-python-headless | 4.11.0.86 | image ops |
| tifffile | 2026.3.3 | TIFF I/O |
| pydantic | 2.14.1 | models |
| PyYAML | 6.0.3 | config |
| mcp | 2.3.0 | MCP server (v2 API) |
| typer | 0.27.3 | CLI |
| PySide6-Essentials | 6.12.0 | GUI |
| structlog | 26.1.0 | logging |
| pytest / pytest-qt / pytest-cov | 9.1.1 / 4.5.0 / 7.1.0 | tests (dev extra) |
| ruff / mypy / pip-audit | 0.17.1 / 2.4.0 / 2.10.1 | quality (dev extra) |
| arena_api | 2.10.0 | vendor wheel from `WS/SDK/lucid_arena/ArenaSDK_Linux_x64/OutputDirectory/Linux/x64Release/ArenaPy/` |
| ebus_base, ebus_edge, ebus_receive | 7.0.1 | vendor wheels from `WS/SDK/jai_ebus/` |

### Tasks
1. Write `APP/pyproject.toml`:
   - hatchling build, src layout, name `camera-lab`, `requires-python = "==3.12.*"`
   - runtime deps above, with `numpy>=1.26,<2` and `opencv-python-headless>=4.11,<4.12`
   - `[project.optional-dependencies] dev = [...]`
   - `[project.scripts] camera-lab = "camera_lab.cli:app"`
   - ruff, mypy and pytest config (markers: `hw`; `addopts = "-m 'not hw'"`; `qt_api = "pyside6"`)
2. Generate `APP/requirements.lock` with `uv pip compile` (command in the Instructions). Confirm `numpy==1.26.4`.
3. Write `APP/scripts/setup_env.sh` (idempotent, `set -euo pipefail`). It must:
   - install the lock
   - `uv pip install -e APP --no-deps`
   - install the 4 vendor wheels by absolute path
   - print versions
   Run it.
4. Smoke-check the vendor SDKs: `PY -c "import arena_api, ebus_base, ebus_receive"`. Do a read-only discovery of both cameras with a 10-line throwaway snippet (do **not** commit it).
5. Create the skeleton:
   - `camera_lab/__init__.py` with `__version__ = "0.1.0"`
   - `cli.py` with `--version` and empty sub-apps
   - `core/logging.py`
   - `core/paths.py` (roots only)
   - `tests/conftest.py`, plus one trivial test
6. Create `APP/.env.example` (`CAMERA_LAB_LOG_LEVEL`, `CAMERA_LAB_DATA_ROOT`, `CAMERA_LAB_WS_ROOT`). Make sure `.env` is in `WS/.gitignore`.
7. Add an entry to `WS/docs/INSTALL_LOG.md` §6e: what was installed and how.

### Done when:
- [ ] `uv pip list --python PY` shows every version in the table; `numpy` is 1.26.4.
- [ ] `WS/environments/camera_lab/bin/camera-lab --version` prints `0.1.0`.
- [ ] Vendor imports succeed and the throwaway discovery shows both cameras.
- [ ] `pytest`, `ruff`, `mypy` pass on the skeleton.
- [ ] INSTALL_LOG updated.

---

## Phase 2 — Core: safety, paths, config, locks, registry, lights, records, presets
**Complexity:** Complex
**Goal:** All shared, hardware-free core services exist and are fully tested. This is the safety backbone every later phase relies on.

### Dependencies
| Tool / Library | Version | Purpose |
|----------------|---------|---------|
| (Phase 1 env) | — | no new packages |

### Tasks
1. `core/paths.py`:
   - `WS_ROOT` (default `/mnt/workspaces/robotic_cell`, override `CAMERA_LAB_WS_ROOT`)
   - `DATA_ROOT = WS/data/raw_recordings/camera_lab` (override `CAMERA_LAB_DATA_ROOT`)
   - `PRESET_ROOT = WS/config/camera_presets`, `CALIB_ROOT = WS/calibration`
   - `SLUG_RE`, `safe_join(base, *parts)`: resolves the path and raises `UnsafePathError` if the result is outside `base`, any part is absolute, contains `..` or NUL, or fails the slug check.
   - **Security-sensitive → strongest tier + human review.**
2. `core/config.py`: `load_yaml_model(path, Model)` using `yaml.safe_load` + pydantic. The error message names the file and the field.
3. `core/safety.py`. **Security-sensitive → strongest tier + human review.**
   - `SafetyLimits` model loaded from `APP/config/safety_limits.yaml`:
     - `cameras.default` + `cameras.<serial>` overrides:
       - `exposure_us {min,max}`, `gain_db {min,max}`, `frame_rate_hz.max`
       - `allowed_pixel_formats` (optional list)
       - `max_writes_per_s`
     - `lights.default` + per-channel overrides: `max_pct`, `max_step_pct`, `max_changes_per_s`
     - `tuning`: `max_steps_hard`, `max_captures_per_run`
     - `mcp`: `max_capture_count`
   - Functions:
     - `effective_range(spec, limit)`
     - `check(value) -> Accepted | Clamped | Rejected(reason)`, where the reason quotes the YAML key path
     - `RateLimiter` (token bucket, injectable clock for tests)
     - `StepBudget`
     - `limits_hash()`
   - The limits object is **frozen** (pydantic `frozen=True`). There is no setter anywhere.
   - Missing/invalid file → `SafetyConfigError`, and the app refuses to start. `--sim` uses built-in sim defaults.
   - Write `APP/config/safety_limits.yaml` with conservative defaults:
     - exposure 20–200000 µs
     - gain 0–18 dB
     - frame rate ≤ 30
     - lights max 80 %, step 25 %, 2 changes/s
     - max_steps_hard 40, captures/run 200, MCP capture count 5
     - Comment every key.
4. `core/locks.py`: `CameraLock(serial, owner)` as a context manager. It uses an `fcntl.flock` non-blocking lock on `$XDG_RUNTIME_DIR/camera_lab/<serial>.lock` (fallback `/tmp/camera_lab-<uid>/`), writes PID + owner, and raises `CameraBusyError(holder)`. **Security-sensitive.**
5. `core/registry.py`: a generic `Registry[T]` with a `register(name)` decorator, `get(name)` and `names()`. Duplicate names raise.
6. `core/lights/`:
   - `LightChannel` model
   - `LightController` ABC: `channels`, `get_levels`, `set_levels`, `all_off`, `close`
   - `SimulatedLight`: channels from `lights.yaml`, default `white_ring` + `white_bar` + `red` + `blue`, each with a sim `weight`
   - `LightService`, which applies safety (max %, step ramping, rate limit) on top of any controller
   - `atexit` + signal handlers call `all_off()`
   - Write `APP/config/lights.yaml` (`driver: simulated`).
7. `core/records.py`: `RunRecorder.create(camera_info, method, operator, goal, max_steps)`.
   - Lays out the run folder exactly as the Instructions specify (`run.json`, `steps.jsonl`, `captures/`, `previews/`).
   - Provides `add_capture(frame, settings, lights, scores) -> capture_id`, `log_step(...)`, `finish(...)`, `load(run_id)` and `list_runs(filter)`.
   - TIFF via `tifffile.imwrite` (uncompressed); sidecar JSON also stored in `ImageDescription`. Writes are **atomic** (write temp + `os.replace`).
8. `core/presets.py`: `Preset` model, `save(preset, overwrite=False)` (`.bak-<ts>` on overwrite), `load`, `list`. Paths go through `safe_join`.
9. Tests for all of the above (see Instructions "Testing approach": safety, paths, lock-from-second-process via `multiprocessing`, records round-trip, preset `.bak`).

### Done when:
- [ ] There is no code path that mutates a loaded `SafetyLimits`; a test asserts the model is frozen.
- [ ] `safe_join` rejects `..`, absolute paths, NUL and bad slugs (≥ 10 parametrized cases).
- [ ] Rate limiter and step budget are tested with a fake clock.
- [ ] A second process trying to lock the same serial gets `CameraBusyError` naming the holder.
- [ ] A records round-trip is bit-exact for uint8 and uint16 arrays; run.json and sidecar validate against their models.
- [ ] `SimulatedLight` ramps a 0→80 % request in ≤ 25 % steps and calls `all_off` at exit (test with subprocess).
- [ ] Coverage of `core/` ≥ 80 %. All tests, ruff and mypy (strict for core) pass.

---

## Phase 3 — two_d models, driver interface, simulated camera, scoring, processing
**Complexity:** Complex
**Goal:** A complete hardware-free 2D pipeline: open the sim camera → set settings → grab → score, with deterministic metrics.

### Dependencies
| Tool / Library | Version | Purpose |
|----------------|---------|---------|
| numpy | 1.26.4 | — |
| opencv-python-headless | 4.11.0.86 | Laplacian, demosaic, resize, JPEG encode |

### Tasks
1. `two_d/models.py`: all models listed in the layout.
   - `CameraSettings` fields (all optional for patch):
     - `exposure_us: float`, `gain_db: float`, `gamma: float | None`
     - `white_balance: dict[Literal["red","green","blue"], float] | None`
     - `pixel_format: str`, `roi: ROI(x,y,w,h)`, `frame_rate_hz: float | None`
     - `trigger_mode: Literal["off","software"]`
   - `Frame`: `data: np.ndarray`, `pixel_format`, `bit_depth`, `timestamp_utc`, `frame_id`, `settings`.
   - `SettingSpec`: `name`, `kind` (float/int/enum), `min/max/increment/options`, `unit`, `affects_calibration: bool`, `requires_stream_stop: bool`.
2. `two_d/drivers/base.py`: the `Camera2DDriver` / `Camera2D` ABCs from the Instructions. `two_d/drivers/__init__.py`: the vendor→driver table and a lazy-import registry, so `arena` and `ebus` are imported only when used. If an import fails, raise `SdkUnavailableError` (CLI exit 5).
3. `two_d/drivers/simulated.py`: exactly as specified in the Instructions (seeded, physically plausible response to exposure/gain/light, Poisson + read noise, defocus option, Mono8/Mono12/BayerRG8/RGB8). It raises if a format-type change is attempted while streaming, so the stop-stream rule is testable.
4. `two_d/processing.py`: a processor registry with built-ins:
   - `to_float01(frame)` (divide by `2**bit_depth - 1`)
   - `demosaic(frame)` (OpenCV `cvtColor` Bayer→RGB; pick the correct code for the pattern; document it)
   - `to_luma(rgb)` (Rec. 709 weights)
   - `make_preview(frame, long_edge=1024) -> uint8 RGB`
   - `encode_jpeg(img, quality=85)`
5. `two_d/scoring.py`: a metric registry and built-ins. **Scoring rules:**
   - Bayer images are scored **after** demosaic. RGB clip/black checks are per-channel (a pixel counts if **any** channel hits).
   - An optional ROI comes from `scoring.yaml`.
   - Metrics:
     - `histogram`: 256 bins over `[0,1]` of luma; stored as a list in the JSON.
     - `mean_level`; `p01`, `p50`, `p99` of luma.
     - `clipped_pct`: % of pixels with any channel ≥ `full_scale` (raw value == max code).
     - `black_pct`: % of pixels with all channels ≤ `black_threshold` (default 0.02 of full scale, configurable).
     - `sharpness`: variance of `cv2.Laplacian(luma_u8, cv2.CV_64F, ksize=3)` on the 8-bit-scaled luma. Document that it is resolution-dependent and comparable only within one camera + ROI.
     - `noise_sigma`: Immerkær (1996) fast noise estimate on float luma, with the kernel `[[1,-2,1],[-2,4,-2],[1,-2,1]]`: σ = sqrt(π/2) · Σ|I∗N| / (6 (W−2)(H−2)). Report it normalised 0–1.
     - `composite`: in 0–100, = 100 × Π of per-metric sub-scores, each in [0,1]:
       - exposure: `1 − min(1, |mean_level − target| / tolerance)`
       - clipping: `max(0, 1 − clipped_pct / clip_max)`
       - black: `max(0, 1 − black_pct / black_max)`
       - noise: `max(0, 1 − noise_sigma / noise_max)`
       Defaults: target 0.45, tolerance 0.25, clip_max 1.0 %, black_max 5 %, noise_max 0.03.
       Sharpness is reported but **not** in the composite (scene-dependent).
       All values come from `APP/config/scoring.yaml`, optionally overridden per camera and per preset tag.
   - Output is a `ScoreReport` with floats rounded to 6 decimals, so the JSON is deterministic.
6. Tests:
   - golden synthetic images
   - determinism (score the same TIFF twice → identical JSON bytes)
   - sharpness decreases monotonically over defocus σ = 0, 1, 2, 4
   - noise estimate within ±15 % of injected σ ∈ {0.005, 0.01, 0.02}
   - sim camera responds monotonically to exposure, gain and light
   - stop-stream rule enforced

### Done when:
- [ ] Sim camera: open → apply → grab → score works in a test, with no hardware or SDK installed.
- [ ] All scoring tests pass. Composite formula + defaults are documented in `scoring.py` docstrings and `scoring.yaml` comments.
- [ ] Importing `camera_lab.two_d` does not import `arena_api` or `ebus_*` (test with `sys.modules`).
- [ ] Coverage ≥ 80 % on new non-driver modules. All gates pass.

---

## Phase 4 — Service layer, calibration guard, tuning (classic), CLI
**Complexity:** Complex
**Goal:** `TwoDService` is the complete library API, and the CLI exposes it. The classic tuner meets success criterion 5 on the sim camera.

### Dependencies
| Tool / Library | Version | Purpose |
|----------------|---------|---------|
| typer | 0.27.3 | CLI |

### Tasks
1. `two_d/calib_guard.py`: given a camera serial and the names of changed settings with `affects_calibration`, check for `CALIB_ROOT/intrinsics/<serial>*` and `extrinsics/<serial>*` and return warnings. **Read-only**: open nothing for writing (test with a read-only temp dir).
2. `two_d/service.py`, class `TwoDService(limits, light_service, recorder_factory, include_sim)`:
   - `discover()`
   - `open(camera_id, owner)`: takes the `CameraLock` and disables auto functions
   - `close(camera_id)`
   - `get_settings(camera_id) -> (CameraSettings, effective_ranges)`
   - `apply(camera_id, patch, actor) -> ApplyResult`:
     - safety check → stop the stream if needed → driver apply → read back → calibration warnings → log the step
   - `capture(camera_id, count, run_id|None, note, actor) -> list[CaptureResult]`
   - `grab(camera_id, timeout_ms=2000) -> Frame`: the **public, non-recording** frame grab, for the GUI live loop and for other apps (calibration_lab uses it for live capture):
     - The camera must already be open in this process (`open()` holds the lock). Otherwise raise `CameraNotOpenError`.
     - Starts the stream if it is not running. The stream keeps running until `close()` or a stream-stopping `apply()`.
     - Returns a `Frame` whose `data` is a copy (the driver buffer is already requeued). `settings` holds the settings at grab time, as last read back.
     - Writes **nothing**: no run folder, no TIFF/JPEG, no `steps.jsonl` line, and no scoring. Debug-level stderr logging only.
     - Does not consume a tuning-run step and needs no `run_id`.
     - A timeout or incomplete buffer raises the same typed error as `capture()` (exit code 6). Add `CameraNotOpenError` to `two_d/errors.py` with its own error `code`.
     - `capture()` calls `grab()` internally and then records and scores, so there is one grab path.
   - `score_file(path)`
   - `start_run(...)` / `finish_run(...)`
   - presets: `save_preset`, `apply_preset`, `list_presets`
   - All errors are typed exceptions defined in `two_d/errors.py`, each with an error `code` string used by the CLI exit codes and the MCP error shape.
3. `two_d/tuning.py`:
   - `TuningRun`: wraps a `RunRecorder` + `StepBudget`. Every capture, settings change or light change inside a run consumes one step. Once the budget is exhausted, calls raise `StepBudgetExhausted`.
   - `ClassicTuner`:
     1. Search exposure on a log scale (secant on log(exposure) vs mean_level, with bracketing) towards `target_mean`, keeping `clipped_pct ≤ clip_max`.
     2. If exposure hits its effective max while the image is still too dark, raise gain in ≤ 3 dB steps.
     3. If the image is too bright at minimum exposure, lower gain to its minimum, then report "needs less light".
     4. Optional grey-world white balance on the ROI: set red/blue ratios so mean R = G = B, within the effective ranges.
     5. Stop on convergence (|mean − target| ≤ 0.05 × target and clip OK) or when the budget is used up.
     6. Pick the best capture by composite score.
     It never touches lights (lights are the agent's/human's choice in v1). Document this.
4. `two_d/cli.py` + root `cli.py`: every command in the Instructions command table, with `--json` output, documented exit codes, `--help` examples, and results to stdout / logs to stderr.
   Also add `camera-lab light ...` and `camera-lab limits show`.
5. Tests:
   - `CliRunner` for every command and exit code
   - classic tuner convergence from 5 starting exposures {50, 500, 5000, 50000, 190000 µs} on the sim camera (≤ 12 steps, success criterion 5)
   - budget exhaustion
   - calibration warning appears when the ROI changes and a fake calibration file exists
   - `grab()` on the sim camera returns a `Frame` with the right pixel format, size and increasing `frame_id`, and leaves the data root **unchanged** (snapshot the directory tree before and after)
   - `grab()` on a camera that is not open raises `CameraNotOpenError`; a forced timeout raises the capture-timeout error

### Done when:
- [ ] `camera-lab --sim two-d tune SIM-0001 --method classic` converges and writes a complete run folder.
- [ ] Every CLI command has a test, and the exit codes match the table.
- [ ] Calibration guard is tested and does no writes.
- [ ] `TwoDService.grab()` is public, tested, and writes nothing to disk.
- [ ] All gates pass.

---

## Phase 5 — Lucid Arena driver (real hardware)
**Complexity:** Complex
**Goal:** The TRI054S-C works through `TwoDService` with every MVP setting, verified at the bench.

### Dependencies
| Tool / Library | Version | Purpose |
|----------------|---------|---------|
| arena_api | 2.10.0 | Lucid SDK |
| Arena SDK runtime | 1.0.10.11 | `WS/SDK/lucid_arena/ArenaSDK_Linux_x64` (installed) |

### Tasks
1. **Before coding**, read (TOC-first) the Triton TRI054S manual sections for:
   - exposure, gain, gamma, colour processing / white balance (`color_processing`, `white_balance`, `ccm`)
   - pixel formats, ROI, acquisition/frame rate, triggering, bandwidth/stream
   Then read the arena_api API reference entries for `system`, `Device`, `nodemap`, `BufferFactory`, and the examples `py_simple_acquisition.py`, `py_exposure.py`, `py_trigger.py` and `py_enumeration_handlingdisconnections.py` in `WS/SDK/lucid_arena/arena_api-2.9.5/examples/`.
   Write a short node-mapping table into the module docstring, with each row citing the manual section.
2. Implement `ArenaDriver` / `ArenaCamera`:
   - Lucid-only filter
   - discovery from `system.device_infos`, with no device creation for discovery
   - capabilities read live from the node map (min/max/inc/enum entries)
   - auto functions off
   - stream nodes set
   - grab with timeout → copy → requeue
   - Mono/Bayer/RGB and 10/12-bit unpack to uint16
   - stop/start stream around format-type changes
   - `close()` always destroys the device
   - disconnection mapped to `CameraDisconnectedError`
3. `@pytest.mark.hw` tests:
   - discover finds 233100505
   - set/read back exposure, gain, WB, ROI within one increment
   - 10 consecutive captures with no incomplete buffers
   - full-resolution capture + save < 2 s
   - classic tune converges on a static scene within 15 steps
   - lock prevents a second opener
4. Unit tests with a **fake node map** (a dict-backed stub of the arena node API), so mapping logic is covered without hardware.

### Done when:
- [ ] All `hw` tests pass at the bench, with Derek present. Results are pasted into the gate report.
- [ ] The node-mapping table cites manual sections; any manual-vs-device discrepancy is reported.
- [ ] Non-hw suite still passes on a machine with no Arena SDK (driver import is lazy).
- [ ] All gates pass.

---

## Phase 6 — JAI eBUS driver (real hardware)
**Complexity:** Complex
**Goal:** The AP-3200T-PGE works through `TwoDService` with every MVP setting, including prism-camera white balance.

### Dependencies
| Tool / Library | Version | Purpose |
|----------------|---------|---------|
| ebus_base / ebus_edge / ebus_receive | 7.0.1 | JAI SDK (runtime at `/opt/jai/ebus/Ubuntu-24.04-x86_64`) |

### Tasks
1. **Before coding**, read (TOC-first):
   - the eBUS Python API Quick Start Guide
   - the official samples in `/opt/jai/ebus/Ubuntu-24.04-x86_64/share/samples/python_samples/` (device finding, connection, stream, pipeline, GenICam parameters)
   - the JAI AP-3200T-PGE manual v2.2 sections: Pixel Format (L1455), Exposure Mode (L1782–L2130, use the **AP-3200T** subsections), Gain Control (L2131), White Balance (L1234), Gamma (L2284), and the **Setting List (L3261)** for exact node names, ranges and selectors
   Write the node-mapping table into the module docstring, with each row citing the manual line/section.
2. Implement `EbusDriver` / `EbusCamera`:
   - JAI-only filter
   - discovery via `PvSystem` (verified working)
   - connect → open stream → configure packet size 9000 → pipeline → acquisition start/stop
   - auto functions off
   - per-channel WB via the camera's gain selector as the manual specifies
   - RGB pixel formats (unpack 10/12-bit to uint16)
   - always release buffers, stop the pipeline, close the stream and disconnect in `finally`
   - errors from `PvResult` mapped to typed errors
3. Same `hw` test set as Phase 5 for serial U330439, plus a WB test: after grey-world WB on a grey target, the channel means are within 3 % of each other.
4. Unit tests with a fake `PvGenParameterArray` stub.

### Done when:
- [ ] All `hw` tests pass at the bench, with results pasted into the gate report.
- [ ] The Arena driver never opens the JAI camera, and vice versa (tested via the vendor filter with both cameras connected).
- [ ] All gates pass.

---

## Phase 6b — Camera profiles and tuning strategies
**Complexity:** Complex
**Added:** 2026-10-21 by Derek (option "a": before the MCP server, so MCP and GUI are built around profiles).
**Goal:** Each camera has a git-tracked profile (what it is, its operating modes, and per mode: tuning strategy, allowed knobs, scoring targets). Tuning picks a strategy per camera and mode, so camera_lab can grow into a library of profiled cameras that can be compared.

### Why
Cameras differ in ways a single generic tuner cannot handle (bench findings 2026-10-21):
- Lucid TRI054S-C: rolling shutter (static scenes), HDR sensor with 4 HDR channels (each its own exposure + analog gain), HDR merged or single-channel output; in HDR mode ExposureTime/Gain are read-only.
- JAI AP-3200T-PGE: 3-CMOS prism (no Bayer), gain as multiplier with per-sensor R/B gains, optional per-sensor (R/G/B) exposure, ~3 % black-level pedestal.
- Future cameras will be added over time and compared in "shoot-outs"; stereo pairs come later.

### Dependencies
| Tool / Library | Version | Purpose |
|----------------|---------|---------|
| (Phase 1 env) | — | no new packages |

### Tasks
1. **Advanced settings.** Add `advanced: dict[str, float | str | bool]` to `CameraSettings` and `CameraSettingsPatch` for vendor/mode-specific settings. Each advanced setting has a `SettingSpec` in `capabilities()` (named like `hdr.enabled`, `hdr.output`, `hdr.ch0.exposure_us`, `hdr.ch0.gain_db`, `prism.exposure_mode`, `prism.exposure_red_us`). `TwoDService.apply` validates them exactly like core settings (camera range ∩ safety limit; reject/round; read back; log).
2. **Safety limits for advanced settings.** By unit: `us` → `exposure_us` limits, `dB` → `gain_db` limits; optional per-name overrides in `safety_limits.yaml` (`cameras.<serial>.advanced.<name>: {min, max}`), human-edited only, like all limits.
3. **Driver support.**
   - Arena (TRI054S-C): HDR nodes (`HDRTuningEnable`, `HDROutput`, `HDRTuningChannelSelector` + `HDRChannelExposureTime` / `HDRChannelAnalogGain` per channel, `HDRImageEnhancementEnable`, `HDRDigitalClampingEnable`). Verify each against the live node map (the manual lists names only, `HDR Control` L2165).
   - eBUS (AP-3200T-PGE): individual exposure (`ExposureTimeMode` = Individual, `ExposureTimeSelector` R/G/B) and individual gain mode (`IndividualGainMode`), per manual Exposure Mode L1782 and Gain Control L2131; verify live.
4. **Profiles.** `APP/config/camera_profiles/<serial>.yaml`, pydantic-validated (`yaml.safe_load`):
   - `characteristics`: shutter (`rolling` | `global`), colour (`bayer` | `prism` | `mono`), `hdr_capable`, measured `black_level` (fraction of full scale), free-text notes.
   - `default_mode` and `modes.<name>`: `strategy`, `settings` (a patch applied when entering the mode), `knobs` (settings the tuner and the agent may change in this mode), `scoring` (overrides of `scoring.yaml` for this mode), `description`.
   - Profiles for 233100505 (modes `hdr` [default], `linear`), U330439 (mode `prism`) and SIM-0001 (mode `linear`). A camera without a profile gets a generic `linear` profile.
5. **Strategies.** A registry `two_d/strategies/` (decorator-based, like drivers and metrics). Every strategy uses the existing `TuningRun` step budget and the service's safety flow, records like the classic tuner, and never touches lights.
   - `linear`: the existing classic tuner (pedestal-aware secant, clip-limited stop, grey-world WB).
   - `prism`: `linear` plus white balance by gain (default) or by per-sensor exposure (`wb_method: exposure`).
   - `hdr`: v1 tunes the HDR channels' shared exposure by bisection to keep `clipped_pct` within the mode's limit while maximising the mode's composite score; channel gains stay at the mode's settings. Scoring for HDR modes targets "no clipping + enough shadow detail", not a mean of 0.45. Refine with bench data (the manual does not document HDR behaviour).
6. **Service + CLI.** `TwoDService.get_profile(camera)`, `set_mode(camera, mode, actor)` (applies the mode's settings through `apply`), `tune(camera, mode=None, ...)` choosing the strategy from the profile. CLI: `two-d profile show <camera>`, `two-d mode set <camera> <mode>`, `two-d tune <camera> [--mode M] [--strategy S]` (`--method classic` stays and means non-AI tuning; the strategy comes from the profile unless `--strategy` is given). Opening a camera does **not** change its mode.
7. **Tests.** Unit tests with the fakes for every advanced setting, profile loading/validation, strategy selection, each strategy on the simulator (sim gets an HDR-like and a prism-like mode), and the safety flow for advanced settings. `hw` tests: enter each mode on each camera, tune in each mode, and read back.

### Done when:
- [ ] Profiles for the three cameras load and validate; an unknown camera gets the generic profile.
- [ ] Advanced settings go through the same safety flow (tests for reject, round, read-back, limits by unit and per-name override).
- [ ] `two-d tune` picks the strategy from the camera's profile and mode; each strategy is tested on the simulator.
- [ ] `hw`: the TRI054S-C can enter `hdr` and `linear` modes and be tuned in each; the AP-3200T-PGE tunes in `prism` mode with both WB methods. Results pasted into the gate report.
- [ ] All gates pass.

---

## Phase 7 — MCP server + agent-driven tuning
**Complexity:** Complex
**Goal:** Claude can drive a full tuning run through MCP inside the safety limits (success criterion 6).

### Dependencies
| Tool / Library | Version | Purpose |
|----------------|---------|---------|
| mcp | 2.3.0 | `from mcp.server.mcpserver import MCPServer, Image` |

### Tasks
1. **Security-sensitive phase → strongest tier + human review of the tool surface.**
2. `core/mcp_server.py`:
   - `server = MCPServer(name="camera-lab", instructions=WORKFLOW_TEXT, lifespan=...)`
   - The lifespan builds `TwoDService` + `LightService` once, and closes cameras and turns lights off on shutdown.
   - Register exactly the tools in the Instructions table with `@server.tool()`.
     - Arguments are pydantic models with bounds.
     - Results use the `{"ok": true, ...}` / `{"ok": false, "error": {code, message}}` shape.
     - `capture` also returns `Image(data=jpeg_bytes, format="jpeg")` of the preview.
   - `camera-lab mcp [--sim]` calls `server.run(transport="stdio")`. **No other transport is allowed.**
   - Logging goes to stderr only.
   - Each MCP call is logged into the active run's `steps.jsonl` with `actor="agent"`.
3. Write `WORKFLOW_TEXT`:
   - the step-by-step loop
   - what each score means and the targets from `scoring.yaml`
   - that rejections are normal feedback
   - that limits cannot be changed
   - to call `lights_off` / `finish_tuning_run` when done
4. Create `APP/.mcp.json` (see Instructions).
5. Tests (in-process client session from the mcp SDK):
   - each tool: valid / boundary / invalid input
   - error shape
   - no tool name or argument can modify limits (introspect the tool list)
   - `capture` count bound
   - path-like inputs rejected
   - stdout cleanliness (spawn `camera-lab mcp --sim` as a subprocess, do the initialize handshake, assert every stdout line is valid JSON-RPC)
6. Manual acceptance:
   - Derek opens Claude Code in `APP/` and asks Claude to tune `SIM-0001`, then the TRI054S-C.
   - Check the resulting run folders + preset.

### Done when:
- [ ] All MCP tests pass; the stdout-cleanliness test passes.
- [ ] A human-reviewed list of the tools and their bounds is approved by Derek.
- [ ] A real Claude session completes one sim run and one hardware run, each with ≥ 3 captures and a saved preset (success criterion 6).
- [ ] All gates pass.

---

## Phase 8 — Desktop GUI (PySide6, dark mode)
**Complexity:** Complex
**Goal:** A human-facing bench GUI: live view, controls, scores, presets and run browser (success criterion 7).

### Dependencies
| Tool / Library | Version | Purpose |
|----------------|---------|---------|
| PySide6-Essentials | 6.12.0 | QtWidgets |
| pytest-qt | 4.5.0 | GUI tests |

### Tasks
1. **Read `APP/spec/standards/gui-coding-standards.md` first. Follow it exactly.**
   - Pure-code layouts; every `QWidget` uses the 5-method `__init__`
   - named widgets and layouts; no magic numbers
   - all styles in `two_d/gui/styles.py` (dark palette §9)
   - slots named `_on_<widget>_<event>`
2. `gui/workers.py`: a `CameraWorker(QObject)` moved to a `QThread`.
   - Live loop: `service.grab()` → preview → fast scores (mean, clipping, histogram on the preview) → emit `frame_ready(QImage, dict)`.
   - Full capture + full scores on demand.
   - The UI thread never calls the SDK directly.
   - Throttle preview to ≤ 15 fps.
3. Widgets, each in its own file under `gui/widgets/`:
   - `CameraListPanel`, `LiveViewWidget` (with clipping-overlay toggle), `HistogramWidget` (QPainter)
   - `CameraSettingsPanel`, `LightPanel`, `ScorePanel`, `RunBrowserPanel` (with 2-capture compare)
   - `PresetDialog`
   Use the dynamic-widget factory table in the Instructions (factory + populate + clear).
4. `MainWindow`:
   - toolbar actions as specified, status bar (state, fps, lock holder, last warning)
   - closing the window stops workers, closes cameras and calls `lights_off`
5. `camera-lab gui [--sim]` entry point.
6. pytest-qt tests (offscreen):
   - window opens with `--sim`
   - the camera list populates
   - moving the exposure slider calls `service.apply` with the right value
   - out-of-limit typed values show the rejection reason
   - populate → clear leaves zero child widgets
   - dark palette is applied
   - closing the window releases the lock
7. **Human review step:** Derek runs the GUI on both real cameras and approves the layout before the gate.

### Done when:
- [ ] Pre-submit checklist from `gui-coding-standards.md` §10 is ticked in the gate report.
- [ ] Live view ≥ 5 fps on the TRI054S-C (was 10 fps; changed 2026-10-21), with no UI freeze > 200 ms (measure with a `QElapsedTimer` heartbeat and log it).
- [ ] All GUI tests pass offscreen. Derek approves the layout.
- [ ] All gates pass.

---

## Phase 9 — Security hardening, docs, polish
**Complexity:** Moderate
**Goal:** The release-quality bar from all success criteria is met, and the docs are current.

### Dependencies
| Tool / Library | Version | Purpose |
|----------------|---------|---------|
| pip-audit | 2.10.1 | CVE audit |

### Tasks
1. Run `pip-audit` on the env and resolve any high/critical findings without breaking `numpy<2`. Report if that is impossible.
2. Security review (strongest tier): walk the `security-standards.md` §9 checklist. Grep for stray `print(` outside the CLI output functions, `yaml.load(`, string-built paths and broad `except:`. Fix and report.
3. Complete the coverage targets (success criterion 8).
4. Docs:
   - update `APP/README.md` (install, CLI cheat-sheet, MCP setup, GUI)
   - update `APP/src/camera_lab/two_d/README.md` (status: implemented; close the open questions)
   - update `APP/CONTEXT.md`
   - add a `WS/docs/INSTALL_LOG.md` entry
   - add `docs/FileStructure.txt` lines for `config/camera_presets/`
5. Run the full success-criteria checklist and paste the evidence (command + output) into the final report.

### Done when:
- [ ] Every success criterion 1–9 has evidence in the report.
- [ ] `pip-audit`: no high/critical findings. Security checklist complete and human-reviewed.
- [ ] Docs updated. All gates pass.

---

## Phase 10 — Test charts and controlled bench tests
*Added by Derek, 2026-10-21.*
**Complexity:** Complex
**Goal:** Compare cameras against known targets instead of whatever is in the scene. Charts form a growing **chart library** (like the camera library). Every chart carries a machine-readable marker, so the software and the agent can record which charts were used in each capture.

### Why
- On 2026-10-21 the JAI white-balance bench tests failed because daylight changed during the run (sunrise). Grey-world scores depend on the scene; known targets under controlled light do not.
- Most machine-vision cameras have fixed lenses. Focus is set once at install, so the sharpness chart is a **resolution chart** (MTF50) that compares cameras and lenses and checks that a lens has not drifted.

### Dependencies
No new packages. AprilTag 36h11 comes from `cv2.aruco` (OpenCV 4.11.0, verified: 587 tags, `ArucoDetector`). PDFs are written directly (vector rectangles, polygons and text only), so no PDF library is needed.

### Chart library
- One file per chart: `APP/config/charts/<chart_id>.yaml`. It holds the type, version, paper size, the AprilTag id, the tag's printed size and position, the feature layout relative to the tag (patch boxes, edges, star centre), and notes (print date, paper, printer, or "bought: …").
- Chart ids are slugs, e.g. `step-wedge-letter-v1`. A reprint or a changed layout gets a new version and a new id. Old results stay traceable.
- **Tag ids:** camera_lab charts use AprilTag 36h11 ids **100–199**. Other ids are left for the calibration app and other users, so the two apps never mix up targets.
- Bought charts (grey card, colour checker) join the library through a printed tag card placed next to them.
- Generated PDFs go to `APP/docs/charts/<chart_id>.pdf`. They are printed at 100 % scale (no "fit to page"). Each page carries a 50 mm scale bar for checking the print.

### Tasks
1. **Chart generator (first, so charts can be printed straight away).** `two_d/charts/`: chart models and loader (`yaml.safe_load` + pydantic, like the profiles), a minimal vector PDF writer, and one generator per type. The first Letter (8.5 × 11 in) charts are:
   - `step-wedge-letter-v1`: grey step wedge (20 steps, linear in nominal reflectance)
   - `resolution-letter-v1`: slanted edges (~5°, horizontal and vertical) + Siemens star
   - `grey-patch-letter-v1`: large neutral grey patch
   - `tag-card-letter-v1`: tag-only marker cards (several ids) for flat-field panels, part trays and bought charts

   Every chart has its AprilTag(s), a human label (id, version, type), the 50 mm scale bar and a print note. CLI: `camera-lab charts list | show <id> | make <id> [--out]`. Tests: models, id range, PDF is valid and has the right page size, and a rendered tag in the generator's own raster is detected with the right id.
2. **Chart detection.** `detect_charts(frame)` finds AprilTags (36h11, ids 100–199), maps them to library charts and returns id, version, tag corners and pose in the image. Every capture records the charts it saw in its sidecar and `run.json`. MCP: captures report the charts seen.
3. **Chart scores** (metric plugins, used only when the chart is seen): step wedge (number of steps resolved, linearity, black and clip points); slanted edge (MTF50 in cycles/pixel and line pairs/mm using the chart scale); grey patch (white-balance error of a neutral, exposure level). Patch and edge locations come from the tag pose plus the chart layout; nobody draws boxes.
4. **Camera settings backup and restore** (before anything else can change a camera).
   - Before the first write in a session, camera_lab saves a full backup of the camera's settings: every readable feature and its value (vendor node map), plus camera_lab's own settings view. It goes to `WS/config/camera_backups/<serial>/<UTC timestamp>.json`, never overwritten.
   - `camera-lab two-d backup <camera>` makes one on demand; `camera-lab two-d restore <camera> <backup>` writes it back (writable features only, in a safe order, read back and reported per feature). Restore asks for confirmation.
   - The mechanism (GenICam feature streaming vs. node-by-node) is chosen per SDK after checking the Arena and eBUS manuals; node names are verified, never guessed.
   - On close, camera_lab leaves the camera as configured for the bench: it reloads the camera's start-up user set (see step 5). It no longer leaves auto functions switched off with no record (2026-10-21: the TRI054S-C was left in HDR mode at 10 ms in a dark room and ArenaView showed an almost black image).
4b. **Limits policy change** (Derek, 2026-10-21; see Instructions *Limits policy*). Security-sensitive: strongest tier + human review.
   - `safety_limits.yaml` schema 2: drop the camera `exposure_us` / `gain_db` / `frame_rate_hz` / per-name `advanced` caps; keep `max_writes_per_s` (and optional `allowed_pixel_formats`); add `protected_features` (wildcards). Lights, tuning and MCP sections unchanged. Schema 1 files are rejected with a message that explains the change (fail closed).
   - Camera profiles gain an optional `working_range` per setting (guidance). The classic tuner and strategies steer inside it; `get_settings` / MCP results report it next to the camera range.
   - The service rejects writes to protected features (settings, advanced settings, restore); a restore skips protected features whose value differs and lists them in its report.
   - Update tests (criterion 2), `limits show`, the MCP `get_safety_limits` result, README and CONTEXT. Then restore the TRI054S-C's own backup as the live check.
5. **Camera setup procedure** (Derek, 2026-10-21). A guided, gated sequence with **live video shown the whole time** (GUI "Camera setup" window; each step also callable from the CLI/MCP). Every step **passes or stops** and says what it needs from the human. The app never guesses at causes it cannot see (iris, lighting, focus ring).
   1. **Backup** (step 4).
   2. **Light stable?** Two captures a few seconds apart; chart-patch levels must agree within a tolerance. Fail → "lighting is not stable".
   3. **Brightness**, judged on the grey patch / step wedge (not the whole scene): tune exposure (then gain, within the safety limits and the profile's mode) to the target level with no clipped wedge steps. Fail → "too dark at max exposure X / gain Y: open the iris or add light", or "clipping at minimum exposure: close the iris or reduce light". Stop.
   4. **Focus** (human): live sharpness (MTF50 from the resolution chart) with its best-so-far value while Derek turns the focus ring; Derek locks the ring and confirms. The first result is recorded as this camera + lens baseline; later runs compare against it (a drop beyond tolerance → "focus has drifted or the lens was bumped"). Fail → stop.
   5. **Brightness re-check** (focus or iris may have moved). Fail → back to 3.
   6. **White balance** (colour cameras): grey-patch channel spread within tolerance (prism: by gain or exposure per the profile). Fail → stop.
   7. **Save:** after Derek confirms, save the settings to the camera's **User Set 1** and make it the start-up set; also save a camera_lab preset and the full setup report (all step results, chart scores, backup path) in the run folder. Writing camera memory **always needs confirmation**; it is never automatic. User-set node names are verified per camera (Lucid and JAI differ); settings that a user set does not store are listed in the report.
6. **Controlled bench guidance** in `docs/`: blackout enclosure, steady LED lighting, fixed mounting plate with stops at the working distance, matte prints. Office prints are fine for relative tests (camera A vs B, today vs last week); absolute values need bought references (18 % grey card, colour checker).

### Done when:
- [ ] Step 1: the four Letter PDFs are generated, printed by Derek and the scale bar measures 50 mm ± 0.5 mm.
- [ ] Charts are detected on all three bench cameras with the right id, and recorded in the run.
- [ ] Chart scores are deterministic (same image → same JSON) and covered by tests with synthetic chart images.
- [ ] Backup → change → restore returns every writable feature to its backed-up value (tested on a fake camera and on each bench camera).
- [ ] Limits policy: camera settings are limited only by the camera's range; lights, rates/budgets and protected features stay hard limits (tested); the TRI054S-C backup with HDR gains 19.8 / 29.4 dB restores. Derek reviews the new `safety_limits.yaml`.
- [ ] The setup procedure runs on each bench camera with live video: each failure case stops with its message (tested with the simulated camera), and a passing run ends with a confirmed User Set 1 save and a setup report.
- [ ] All gates pass. Derek approves the setup window.

---

## Phase 11 — Camera fingerprint (characterisation)
*Added by Derek, 2026-10-21, after the first bench day (`docs/experiments/20261021_findings.md`).*
**Complexity:** Complex
**Goal:** Every new camera in the library is measured once, the same way, by Derek and camera_lab together. The result — the camera's **fingerprint** — replaces guessed limits: it sets the working ranges, the knob order, the gain limit for a required signal-to-noise ratio, and the dark/flat corrections. Cameras become comparable like for like.

### Why (evidence from 2026-10-21)
- Guessed knobs failed: the TRI054S HDR setup only had exposure knobs and stopped "too dark"; Derek fixed it by hand with gain.
- Spatial σ on a printed patch measures print texture and the light gradient, not camera noise. Gain limits need **temporal** noise (two frames differenced).
- The TRI054S was quieter at 18 dB than at 12 dB (a possible gain-mode switch). Only a measurement finds that.
- 8-bit hid the TRI054S noise floor. Fingerprints use the camera's 12-bit format.
- User sets differ per camera (the JAI refuses UserSetSave while streaming; TRI054S user sets don't store HDR; the JAI boots from User Set 1 without a boot-set feature). These belong in the fingerprint.
- Stale frames after a change needed 2 frame periods on the TRI054S, 1 on the TRI089S-M.

### Tools
- Lens cap (dark frames).
- Opal acrylic held close to the lens so it fills the view (flat field). It does not need to be in focus. Hold it still during each burst; the software checks the field is even and steady, and asks again if not.
- The Letter charts (step wedge, resolution) for the scene tests. No new packages.

### Procedure (gated, like the setup: each step passes or stops and says what it needs; live video shown)
1. **Backup** (Phase 10 step 4) and record the camera identity (model, serial, firmware, sensor size, formats).
2. **Timing:** fps per pixel format at full frame; exposure → frame-rate behaviour; how many frame periods stale frames last after a write (frame timestamps); which features are locked while streaming.
3. **Dark frames (cap on):** at the camera's 12-bit format, for a grid of gains (e.g. 0, 6, 12, … up to the camera max) and exposures (short, one frame, long): black level (pedestal), temporal read noise (frame difference), dark fixed-pattern noise, hot/dead pixel list. Stored as a master dark per setting where useful.
4. **Flat field (opal acrylic):** an exposure sweep from dark to saturation at low gain, two frames per point:
   - linearity (signal vs exposure) and the saturation level;
   - photon transfer (temporal variance vs mean signal) → system gain (DN per electron), full well and read noise in electrons;
   - pixel response non-uniformity and vignetting → a master flat.
5. **Noise vs gain** at constant output (flat field): temporal SNR per gain step, finding gain-mode switches (e.g. the TRI054S's 18 dB jump).
6. **Scene checks (charts):** step wedge steps resolved and resolution MTF50 at the working distance, with and without the corrections.
7. **User sets and power-up:** what a user set stores (`UserSetFeatureSelector` where it exists), whether a save needs the stream stopped, which set the camera boots from (Derek power-cycles; camera_lab reads it raw).
8. **Report and fingerprint file:** `WS/config/camera_fingerprints/<serial>/fingerprint.yaml` (+ `dark_*.npz`, `flat.npz`), never overwritten (dated versions). The report shows every table and plot; Derek reviews it.

### Strict procedure (Derek, 2026-10-22: "humans are unpredictable — be clear, add steps if needed")
The fingerprint of a camera **model** is the reference the local AI agent uses for that model, so every run must be repeatable. The human never improvises a setting; every setting is dictated and then **measured**.
- **Each fingerprint records first:** camera model, serial number and firmware (`DeviceFirmwareVersion`, not the discovery "version"), then the lens and the iris per step.
- **Fixed reference state** for the whole run: the lens named in the recipe, **iris fully open** (a mechanical end stop, so repeatable), the bench light at the recipe's reference setting. Focus is not needed (the cap and the diffuser make focus irrelevant; the diffuser works because it is out of focus).
- **Checklists:** every step lists what the human must have done (e.g. "Lens cap on", "Iris set to f/8"); **Run** is enabled only when every item is ticked, and the ticks reset after a STOP.
- **Light recipe per camera model** (in the camera profile, `fingerprint:`): the lens, the reference light (dimmer setting + f-number) and, for HDR cameras, up to four **channel stages** — each a dimmer setting + f-number + the channels measured there. A stage is its own step row (5a–5d). The **bench light table** (`config/fingerprint_bench.yaml`: relative light per dimmer setting, measured 2026-10-22; the lenses and their marked f-stops) turns a stage into an expected light level.
- **Measured verification:** before a stage measures anything, camera_lab measures the light (combined output, linear) and compares it with the recipe's expectation (dimmer ratio × (f_ref / f)²). Outside ±35 % → STOP "the light is N× what dimmer D, f/F should give: check the dimmer and the iris". A channel that still does not fit in a correct stage → STOP "the recipe is off by N× (≈ k f-stops): send this message to the engineer" (the human does not adjust on their own).
- **Light back:** after the stages, a step requires the reference state again (iris fully open, reference dimmer) and verifies it within ±12 % of the light measured in the flat step; nothing after it runs until it passes.
- **No recipe** (a new model): HDR channels run in **discovery mode** — one stage that reports, per channel, how much less/more light it needs (as a factor and in f-stops). Those numbers become the model's recipe.
- **Messages are copyable** (selectable text and a "Copy messages" button) for email or chat.
- **One row per bench state** (Derek 2026-10-22: "from a human's point of view these are one test"): 1 lens cap on (identity, darks), 2 diffuser on at the reference (timing, flat, the HDR channels that need the reference light, noise vs gain), 3–5 one row per iris/light change of the recipe (its HDR channels), last: iris back open (light check, user sets, report). The cap goes on and off once, the diffuser once; one Run per row, however long it takes. Unused rows are not shown.
- **Repeats:** darks, the flat and each HDR channel are measured **3×** in one Run; the median is used and the spread reported; > 10 % spread → "NOT repeatable" in the row and the report (photon-transfer read noise is excluded from the check — it is a shot-noise-dominated intercept; the darks measure read noise directly).
- **Skip:** only a light-change row (HDR channels) can be skipped, after a STOP, with a reason recorded in the report.
- **Modules by sensor type** (Derek 2026-10-22): every camera gets the **core** (cap: identity, darks; diffuser: timing, flat, noise vs gain; finish: user sets, report) so cameras stay comparable; the profile's `characteristics` add modules — **colour planes** for Bayer and prism sensors (each raw site / sensor: response relative to green, temporal noise, PRNU; Bayer green imbalance Gr − Gb; same diffuser frames, no extra human setup), **HDR channels** where `hdr_capable`. Identity flags a profile whose declared sensor colour does not match the camera's raw format. **Prism alignment is not tested:** assumed good (factory-aligned; Derek 2026-10-23). Code: `two_d/fingerprint/` — `common` (types, constants), `base` (camera helpers), `plan` (rows from sensor type + recipe), `core`, `colour`, `hdr`, `report`, `session` (ties them together).
- **Lens transmission:** fast lenses pass more light when stopped down than the nominal (f_open / f)² (Kowa 8 mm: f/2.8 = 0.357 of open, not 0.25); the bench file keeps measured transmission per stop.

### Bench vs. machine (Derek, 2026-10-21)
The bench proves the workflow. Intrinsic and extrinsic calibration is done **on the machine, after the cameras are mounted** (separate calibration app). So the fingerprint marks each result:
- **Camera-only — carries over:** black level, read noise, dark fixed-pattern noise, hot pixels, system gain, full well, linearity, noise vs gain and gain-mode switches, timing / settle periods, stream-locked features, user-set and power-up behaviour.
- **Lens and installation — re-measured on the machine:** focus baseline (MTF50), px/mm and working distance, vignetting / flat field (they change with aperture, focus and lens), lighting levels, exposure limits for the real object speed. The setup procedure (Phase 10 step 5) is run again on the machine; its focus baseline and the flat field are recorded per installation, not per camera.
- Any lens, aperture or focus change after mounting invalidates the machine calibration; camera_lab only warns (calib_guard), the calibration app re-calibrates.

### What the fingerprint feeds
- **Working ranges** suggested for the camera profile (Derek accepts them; the profile stays human-edited): max gain for a required SNR at the resolution used (downsampling by k improves SNR by k), and the gain steps to avoid.
- **Knob order** per mode (e.g. light → exposure → gain; TRI054S HDR: exposure up to one frame, gains fixed).
- **Application limits** in the setup procedure: static scenes have no exposure limit; for moving objects, max exposure = allowed blur (px) / (speed mm/s × px/mm), with px/mm from the charts.
- **Corrections** (processor plug-ins, optional per capture): dark subtraction, flat-field, hot-pixel repair, and N-frame averaging for static scenes (verified √N on 2026-10-21).
- **Camera facts** the drivers need (settle periods, stream-locked features, user-set behaviour) are checked against the driver's assumptions and reported if they differ.

### Done when:
- [ ] The procedure runs on the TRI054S-C and the JAI with Derek; both fingerprints and reports are reviewed by Derek.
- [ ] Photon-transfer results are plausible (system gain, read noise and full well within the sensor's published ranges where known) and the noise-vs-gain table matches the 2026-10-21 bench numbers.
- [ ] Dark and flat corrections measurably reduce fixed-pattern noise / vignetting on a test capture; averaging improves temporal SNR by √N.
- [ ] Tests with a simulated camera that has known gain, read noise, dark current, PRNU and vignetting recover those values.
- [ ] All gates pass.

---

## Phase 12 — Site light assessment (production tool)
*Added by Derek, 2026-10-23: "this is really the biggest production tool".*
**Goal:** on a machine there is no test jig — only the camera, whatever it looks at, and the
camera model's bench baseline (`config/camera_models/<model>.md` white paper +
`<model>.yaml` sheet, from the fingerprint). The tool measures the real scene and turns the
numbers into verdicts and actions by comparing them with that baseline.
- **Checks** (pass / warn / fail / info, each with the value, the model's expectation and the
  action): camera state (design mode; linear output: exposure E vs 2E gives × 2.00, × 1.41 =
  gamma LUT); flicker (row banding on a rolling shutter, frame-to-frame changes; band period ×
  the sheet's row time = ripple frequency → mains 100/120 Hz: exposures in whole flicker periods
  or DC light); brightness (exposure for the model's best-SNR level within the budget = one
  frame at the camera's maximum frame rate, or a given maximum → "N× more / less light", never
  gain); SNR at that exposure vs the model's SNR curve; range (clipped, in the noise floor,
  scene contrast vs the camera's dynamic range); distribution (3 × 3 grid, information only).
- **Interfaces:** `camera-lab two-d assess-light <camera>` (CLI), MCP tools
  `assess_site_light` and `get_camera_white_paper` (added to the reviewed tool surface:
  **Derek to review**). The camera is backed up and restored unless `apply` is set.
  Reports: `<data_root>/two_d/site_light/<serial>/<stamp>/report.json + report.md`.
- **First live result (TRI054S-C, floods + acrylic, 2026-10-23):** linear × 1.99, steady light,
  SNR 82 measured vs ≈ 81 predicted by the white paper; found the 24-bit output's 6.93 fps
  GigE limit (exposure budget 144 ms).
- **Real scene (2026-10-23, shiny camera + lens, floods, f/2):** SNR is measured **per
  brightness band** (robust σ of the frame difference per band), because a non-uniform scene
  mixes levels: the white paper's curve then predicts the camera at 13 levels at once —
  measured 1.03× the model (0.56 %: 26 vs 26, 2.3 %: 48 vs 47). Static edges cancel; moving
  edges (vibration) read as noise, so the advice names vibration first.
- **Mains light (2026-10-23, room lights, floods off):** flicker is checked at a short
  exposure (1 ms: detection and frequency, even on a dim scene) and at the working exposure
  (what remains). Detected "≈ 114 Hz (60 Hz mains)" — 120 Hz, coarse because a frame holds
  few cycles — averaging out at the working 208 ms; the recommended exposure snapped to
  100.00 ms = 12 × 8.33 ms; SNR again 1.01× the white paper at 11 levels.
- **Fresh-agent test (2026-10-23):** an agent with only the CLI and the white paper set the
  camera up and reported 11 gaps. The critical one: `raw_hdr` did not pin `Gain`, which in this
  mode is digital after the merge — every 24-bit baseline number had been taken at a stray
  28.6 dB. Fixed: the mode pins gain 0 dB and black level 1, the baseline was re-measured
  (137 dB dynamic range, best SNR near the top), the sheet states its conditions, the site tool
  warns on a gain mismatch and exposes highlights to 80 % within the frame budget, measures
  flicker at the recommended exposure and interpolates the ripple frequency.
- **Never trust the boot state (Derek 2026-10-23):** every tool starts from a known state —
  load the factory **Default** user set (live only, nothing written to flash), apply the
  design mode's **complete** settings, read every one back (`TwoDService.reset_camera`, CLI
  `two-d reset`, MCP `reset_camera`; the site light assessment and the fingerprint do it
  first). The white paper and sheet record what factory Default is (measured: TRI054S Default
  runs auto white balance, LUT on, 8-bit) and the complete design-mode settings; `raw_hdr`
  now pins gain 0, white balance 1/1/1 (BalanceRatio multiplies the raw data) and black
  level 1. Drivers: `load_user_set` (Arena, eBUS, sim). The JAI boots from User Set 1 with no
  boot-set feature, so the load on open is its only known start.
- **Second fresh-agent run (2026-10-23):** it reset first, kept gain 0, verified linearity
  and gave the right verdict (≈ 9× more light). Its findings, fixed:
  - the classic tuner only moves the mode's knobs (`raw_hdr`: exposure; never gain / white
    balance) — it had raised gain to 1.2 dB;
  - one brightness rule: scoring `level_metric` (`mean_level` | `p99`); `raw_hdr` judges the
    highlights (p99 at 0.80, ≤ 0.2 % clipped, black threshold at the 24-bit noise floor); the
    tuner, capture scores, site tool (p99) and white paper agree; tune targets 0.01–0.95;
  - a new process recognises the active mode from the camera's settings (`current_mode`,
    `scoring_params`): CLI capture/score use the mode's targets; `settings get` shows the mode;
  - frame rate: an auto-lowered rate is recognised on the camera and raised to the fastest
    that fits a shorter exposure (free-run when it fits); side-effect changes of exposure,
    frame rate or gain are reported as warnings;
  - `two-d verify` / MCP `verify_camera_state`: the white paper's state checks on the image
    (design-mode settings, gain, linearity, no dark pixel at 0, finest shadow step in the first
    quantization regime);
  - site tool: linearity at E/2 and E (fits a tight budget), flicker bands in sensor rows,
    light-stability check (daylight drift);
  - `settings set --adv KEY=VALUE`; `--json` help says logs go to stderr; backups documented;
  - white paper v11 "On site" section (sequence, which mode for what, the brightness rule, the
    exposure budget per format, focus needs texture); profile comments and black level fixed.
  Still open: the effective frame-rate maximum after a reset read 6.56 fps once (6.93 later);
  no dry-run / no-save option.
- **Next:**
  the "fresh agent" test (an agent with only camera_lab + the white paper sets the camera up).

## Deferred (keep the interfaces open — do not design these out)
| Feature | Hook already in place |
|---|---|
| Real LED controller driver(s), once the hardware is built | `core/lights` registry + `lights.yaml` `driver:` |
| BitFlow CoaXPress + JAI Spark SP-45001-CXP2A | `two_d/drivers` vendor/interface table; datasheet in `Manuals/vision/jai_spark/` |
| RealSense D435i colour, Helios2+ colour streams | new driver modules (`pyrealsense2` / Arena) |
| Per-object / learned scoring thresholds | `scoring.yaml` per-tag overrides; preset `tags` |
| Extra image-processing utilities | `two_d/processing.py` registry |
| Hardware triggering, sequencer modes | `trigger_mode` enum is extendable; `advanced` settings (Phase 6b) |
| Camera "shoot-out": the same scene and goal on 2–3 cameras, scores side by side | run records + profiles |
| Stereo pairs from two cameras (synchronised capture via PTP / action commands) | profiles; calibration stays in the separate app |
| three_d, laser groups | `core` services are group-agnostic |
| Calibration app (multiple board types, board generation, intrinsics/extrinsics) | **separate app**; this module only reads `WS/calibration/` |
| ROS 2 wrapper | `TwoDService` is the import surface |

---

## Agent routing per phase (capability-based, with named defaults)

Route by tier. The strongest tier is the default and the supervisor. Pair the tier with the current default model so this survives model renames.

*Model names verified October 2026. Confirm they are still current if this spec is much older than that.*

| Phase | Recommended tier (current default) |
|-------|-----------------------------------|
| 1 — Environment Setup | mid (Sonnet 5.5 / `claude-sonnet-5-5`) |
| 2 — Core (safety, paths, locks: **strongest**; records/presets/lights: mid) | strongest (Opus 5.5 / `claude-opus-5-5`) + human review for security parts |
| 3 — two_d models, sim camera, scoring | mid (Sonnet 5.5); strongest for the scoring-formula design |
| 4 — Service, tuning, CLI | mid (Sonnet 5.5); strongest for `service.apply` safety flow |
| 5 — Arena driver | strongest (Opus 5.5): hardware + manuals + buffer lifetimes |
| 6 — eBUS driver | strongest (Opus 5.5) |
| 6b — Camera profiles + strategies | strongest (Opus 5.5): hardware modes, safety flow for advanced settings |
| 7 — MCP server | strongest (Opus 5.5) + human review |
| 8 — GUI | mid (Sonnet 5.5) + human review step |
| 9 — Hardening & polish | strongest for security review; cheapest (Haiku 5.5 / `claude-haiku-5-5`) for mechanical doc edits |
| 10 — Test charts | strongest (Opus 5.5) for chart geometry, detection and MTF; mid (Sonnet 5.5) for the CLI and docs |
| 11 — Camera fingerprint | strongest (Opus 5.5) for the measurements (photon transfer, noise, timing); mid (Sonnet 5.5) for the GUI window and report |
| Trivial mechanical edits (any phase) | cheapest (Haiku 5.5) |
| Phase-gate decisions (every phase) | strongest (Opus 5.5), never delegated |

When in doubt, use the strongest tier. Escalate anything questionable.

| Tier | Default model | Model string |
|------|---------------|--------------|
| Strongest | Claude Opus 5.5 | `claude-opus-5-5` |
| Mid | Claude Sonnet 5.5 | `claude-sonnet-5-5` |
| Cheapest | Claude Haiku 5.5 | `claude-haiku-5-5` |

---

## Total estimate
| Phase | Complexity |
|-------|------------|
| 1 — Environment Setup | Simple |
| 2 — Core | Complex |
| 3 — two_d models, sim camera, scoring | Complex |
| 4 — Service, tuning, CLI | Complex |
| 5 — Arena driver | Complex |
| 6 — eBUS driver | Complex |
| 6b — Camera profiles + strategies | Complex |
| 7 — MCP server | Complex |
| 8 — GUI | Complex |
| 9 — Hardening & polish | Moderate |
| 10 — Test charts + controlled bench | Complex |
| 11 — Camera fingerprint | Complex |

---
