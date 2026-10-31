# camera_lab

A bench tool for humans and AI agents (Claude, via MCP) to check, fingerprint and tune the cell's cameras and LED lighting:
capture → score → adjust → repeat, then save the best settings as a preset. Every camera model and every camera + lens
pair is fingerprinted on the bench; the results go into a SQLite catalog, and a white paper per model and per lens is
generated from it for on-site agents. It runs outside the robot-arm system.

For other agents: `WS/Manuals/apps/camera_lab.md` lists every file this app reads and writes (`camera-lab manual`).

Groups: **2D** (implemented), **3D** and **laser** (Gocator, later). One package, one service layer (`TwoDService`), three thin front ends: CLI, MCP server (stdio), desktop GUI.

```
src/camera_lab/
├── core/      shared: safety limits, paths, config, locks, registry, lights, run records, presets, logging, MCP server
├── two_d/     2D cameras: models, drivers (simulated, Lucid Arena, JAI eBUS), scoring, tuning, profiles, service, CLI, gui/
├── three_d/   3D / depth (Helios2+, RealSense depth) — later
└── laser/     laser line profilers (Gocator 2342 / GoMax) — later
```
camera_lab has its own git repo (`apps/camera_lab/.git`, GitHub `dapfromcanada/RobotArmCameraLab`); the workspace repo does not track it.

Folder names `two_d` / `three_d` instead of `2d` / `3d` because Python package names cannot start with a digit.

## Install

The uv venv `WS/environments/camera_lab` (Python 3.12) must already exist. Then:

```bash
/mnt/workspaces/robotic_cell/apps/camera_lab/scripts/setup_env.sh
```

It installs the pinned `requirements.lock`, camera_lab itself (editable) and the vendor wheels from `WS/SDK/` (Arena 2.10.0, eBUS 7.0.1). **numpy must stay 1.26.4** (the eBUS wheels need numpy < 2); the script fails if anything pulls numpy 2. Always `export UV_LINK_MODE=copy`. Install history: `WS/docs/INSTALL_LOG.md`.

## Cameras

Every camera is identified by its serial; aliases are in `config/cameras.yaml`. Each camera has a profile in `config/camera_profiles/<serial>.yaml` (characteristics, modes, tuning strategy).

| Alias | Camera | Serial | Driver | Default mode |
|---|---|---|---|---|
| `triton` | Lucid Triton TRI054S-C (rolling shutter, Bayer, HDR) | 233100505 | Arena | `raw_hdr` |
| `mono89` | Lucid Triton TRI089S-M (global shutter, mono, 4096 × 2160) | 244500230 | Arena | `raw` |
| `apex` | JAI Apex AP-3200T-PGE (3-sensor prism, global shutter) | U330439 | eBUS | `raw` |
| `sim` | built-in simulated camera (needs `--sim`) | SIM-0001 | simulated | `linear` |

Cameras run **free-run** by default. When an exposure needs a longer frame, the driver lowers the frame rate and restores free-run afterwards (and on open).

**Adding a camera:** discover it; dump its node map to `docs/cameras/` and verify every node name against it or the manual; add a profile and an alias; write its bench tests (`tests/two_d/test_<model>_hw.py`); check live view in the GUI and take 3 test captures.

## CLI cheat-sheet

`camera-lab` lives in `WS/environments/camera_lab/bin/`. Add `--json` for machine-readable output; the global `--sim` adds the simulated camera. A camera is a serial or an alias.

```bash
camera-lab two-d discover                         # list cameras
camera-lab two-d profile show mono89              # profile: characteristics, modes, knobs
camera-lab two-d mode set triton hdr              # enter a profile mode
camera-lab two-d settings get apex                # current settings + effective ranges
camera-lab two-d settings set apex --exposure-us 20000 --gain-db 0
camera-lab two-d capture mono89 --count 3 --note "bench check"
camera-lab two-d tune apex --mode prism           # classic (non-AI) tuner; the run is saved
camera-lab two-d runs list                        # recorded runs, newest first
camera-lab two-d preset save apex bench-default --from-capture <run_id>/0002
camera-lab two-d preset apply apex bench-default
camera-lab two-d score image.tiff                 # score an existing TIFF
camera-lab two-d setup triton                     # guided setup: backup, light, brightness, focus, WB, save to User Set 1
camera-lab two-d backup triton                    # save the camera's whole configuration
camera-lab two-d backups triton                   # list backups
camera-lab two-d restore triton <backup_id>       # write one back (asks; current state backed up first)
camera-lab two-d reset mono89                     # factory Default + design mode, every setting read back
camera-lab two-d verify mono89                    # the white paper's state checks on the live image
camera-lab two-d assess-light mono89              # site light vs the model's baseline (judges the part area)
camera-lab two-d part-area show mono89            # the camera's part area (draw it in the GUI's live view)
camera-lab two-d part-area clear mono89           # remove it: the whole image is scored again
camera-lab two-d fingerprint mono89               # the full bench fingerprint (the GUI is easier)
camera-lab two-d boot-set mono89 --slot Default --yes  # which user set the camera boots from
camera-lab two-d reboot mono89 --yes              # DeviceReset: like a power cycle
camera-lab two-d catalog lenses                   # lenses, marked stops, measured light per stop
camera-lab two-d catalog runs U330439             # fingerprint runs in the catalog
camera-lab two-d catalog changes                  # who changed what in the catalog
camera-lab two-d catalog papers                   # regenerate the white papers
camera-lab two-d catalog sections TRI089S-M --load draft.md  # replace a paper's prose (## sections)
camera-lab two-d catalog backup --to <folder>     # dated copy of the catalog
camera-lab manual                                 # WS/Manuals/apps/camera_lab.md for other agents
camera-lab light list                             # also: light set …, light off
camera-lab limits show                            # limits: lights, rates, protected features (read-only)
camera-lab charts list                            # test-chart library (config/charts/)
camera-lab charts make --all                      # printable PDFs → docs/charts/ (print at 100 %)
camera-lab charts score <image.tiff>              # find + score charts in a saved capture
camera-lab charts make field-letter-v1            # the field chart: 9 landscape sheets in one PDF
camera-lab charts field-plan --model TRI089S-M --lens computar_f_1_2_8_1_1_c   # board distance + spacing
camera-lab charts field <image.tiff>              # whole-image sharpness / light / colour map
camera-lab gui                                    # desktop GUI
camera-lab mcp                                    # MCP server on stdio
```

`camera-lab <command> --help` lists every option. Exit codes:

| Code | Meaning |
|---|---|
| 0 | ok |
| 1 | general / device / streaming-state error |
| 2 | invalid request or usage |
| 3 | camera not found, busy (locked by another process), not open or disconnected |
| 4 | rejected by the safety limits |
| 5 | vendor SDK not available |
| 6 | capture timeout / incomplete frame |

## MCP (agent tuning)

`.mcp.json` in this folder registers the server for Claude Code:

```json
{ "mcpServers": { "camera-lab": { "command": "/mnt/workspaces/robotic_cell/environments/camera_lab/bin/camera-lab", "args": ["mcp"] } } }
```

Open Claude Code in `apps/camera_lab/`, approve the `camera-lab` server once, and ask in plain words, e.g. "open the sim camera, tune it, take 3 captures and save the best as a preset". The server is stdio only (no network port); camera_lab never calls the Claude API and needs no key. The agent has the same rights as a human and works under the same limits.

## GUI

```bash
/mnt/workspaces/robotic_cell/apps/camera_lab/launch.sh          # real cameras
/mnt/workspaces/robotic_cell/apps/camera_lab/launch.sh --sim    # simulated camera
```

Dark mode. Left: camera list. Centre: live view (clipped-pixel overlay, part area) and histogram. Under the live view: "Part area" (drag a box around the part, then "Save part area" or "Cancel"), "Clear part area" (asks first), "Show part area" (dashed outline). Scores, the classic tuner and the site light assessment then judge the part. Right tabs: Camera (mode + a row per setting, including HDR channels and prism exposures), Light, Scores, Runs (captures, side-by-side compare). Toolbar: Start/Stop live, Capture, Run classic tune, Save/Apply preset, Load camera defaults... (asks first; the same reset as `two-d reset`: backup, factory Default, design mode, every setting read back; any setting that did not take is shown), All lights off, Camera setup..., Camera fingerprint..., Focus..., Disconnect. The fingerprint window walks the bench rows with a checklist per row; pick the lens from the list or "Add a lens". The Focus window shows live MTF50 per plane and the best value so far (reset when the chart moves). Setup, fingerprint, focus and Disconnect exclude each other. Messages are also logged to `WS/data/raw_recordings/camera_lab/two_d/gui_messages.log`. Camera work runs in a worker thread; closing the window closes the cameras and turns the lights off.

## Where things go

- **Catalog (source of truth):** `WS/config/camera_catalog.sqlite`; dated copies in `WS/config/camera_catalog_backups/` after every fingerprint, Add a lens, part area save / clear, `catalog import` and before a schema migration (set `CAMERA_LAB_CATALOG_BACKUPS` to put them on another disk)
- White papers (generated from the catalog): `config/camera_models/<model>/camera.md|yaml`, `lenses/<lens>.md|yaml`
- Fingerprint runs: `WS/config/camera_fingerprints/<serial>/<stamp>/` (frames, `fingerprint.yaml`, `report.md`)
- Bench (lights, lenses, shortest exposures): `config/fingerprint_bench.yaml`
- Run records (images, settings, scores): `WS/data/raw_recordings/camera_lab/two_d/<run>/`
- Focus baselines (camera + lens + working distance): `WS/config/camera_setup/<serial>.json` (written by the first setup)
- Camera settings backups: `WS/config/camera_backups/<serial>/<id>.json` (made automatically before camera_lab first changes a camera in a session; never overwritten)
- Presets: `WS/config/camera_presets/` (the old file is kept as `.bak-<timestamp>` on overwrite)
- Safety limits / lights / scoring / aliases / profiles: `config/` (read at start-up; edit and restart)
- Calibration: `WS/calibration/` — read only; camera_lab warns when a change may invalidate it
- Test charts: `config/charts/<chart_id>.yaml` (library; AprilTag 36h11 ids 100–199; field chart sheets 130–138), PDFs in `docs/charts/`
- Camera node-map dumps: `docs/cameras/`. Bench findings: `docs/experiments/`. Spec: `spec/` (read-only). Context for agents: `CONTEXT.md`

## Tests

```bash
cd /mnt/workspaces/robotic_cell/apps/camera_lab
QT_QPA_PLATFORM=offscreen ../../environments/camera_lab/bin/python -m pytest -q   # no hardware
../../environments/camera_lab/bin/python -m pytest -m hw -q                       # at the bench
```

Bench tests skip cameras that are not connected. The TRI054S HDR-mode tests need `CAMERA_LAB_HW_TRITON_MODES=1`.

## Security

**Threat model.** *Protected:* the hardware (LEDs, cameras) from unsafe settings, the run records and presets on disk, and the calibration files. *Threats:* a mistaken or misled agent issuing bad settings, malformed input from the CLI, MCP or GUI or from YAML files, path traversal through names and ids, a compromised dependency. *Trust boundaries:* CLI arguments, MCP tool calls, GUI input, YAML config files, files read back from the run folders, vendor SDK responses.

**Controls.**
- **Limits policy (schema 2, 2026-10-21):** `config/safety_limits.yaml` is enforced in `core/safety.py` only, on every path (CLI, MCP, GUI, tuners), read-only at runtime, and the app refuses to start if it is invalid (fail closed; `--sim` uses built-in defaults). Camera exposure, gain and frame rate are limited only by the camera's own range; each profile's `working_range` steers tuning but never blocks. Hard limits: lights (max %, step, rate), rates and budgets, and **protected camera features** (network/IP, reset, firmware/file access, user-set save), which are never written by settings tools or a restore.
- All input is validated with pydantic. Names and ids are slug-validated and every path is built with `safe_join` (no string-built paths). YAML is parsed with `yaml.safe_load` only, size-limited.
- No shell, `eval` or pickle. No secrets are needed (`.env.example` lists the optional variables; `.env` is git-ignored).
- MCP is stdio only (no network listener). Errors returned to the agent carry no stack traces (those go to the stderr log). Per-camera file locks in `/run/user/<uid>/camera_lab/` (else `/tmp/camera_lab-<uid>/`, chosen from the uid, never from `XDG_RUNTIME_DIR`) stop two processes driving one camera.
- `pip-audit` is clean (2026-10-21). The vendor SDK wheels are not on PyPI and cannot be audited.
