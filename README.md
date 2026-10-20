# camera_lab

AI-assisted camera and lighting tuning app: capture images from each camera, score them, adjust camera and LED settings, repeat.

Groups: **2D**, **3D**, **laser** (Gocator, later). Shared core: device registry, light control, run records, MCP tools for Claude.

```
src/camera_lab/
├── core/      shared: device registry, light control, run records, safety limits, MCP tools
├── two_d/     2D cameras (JAI, BitFlow, Lucid/RealSense colour streams)
├── three_d/   3D / depth (Helios2+, RealSense depth)
└── laser/     laser line profilers (Gocator 2342 / GoMax)
```
Folder names `two_d` / `three_d` instead of `2d` / `3d` because Python package names cannot start with a digit.

- Spec files (from spec builder): `spec/`
- Code: `src/`   Tests: `tests/`
- Environment: `environments/camera_lab` (uv, Python 3.12)
- Run records (images + settings + scores): `data/raw_recordings/camera_lab/`
- Calibration results go in `calibration/`, not here (see docs/FileStructure.txt)
