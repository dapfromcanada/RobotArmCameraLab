# camera_lab

AI-assisted camera and lighting tuning app: capture images from each camera, score them, adjust camera and LED settings, repeat.

Groups: **2D**, **3D**, **laser** (Gocator, later). Shared core: device registry, light control, run records, MCP tools for Claude.

- Spec files (from spec builder): `spec/`
- Code: `src/`   Tests: `tests/`
- Environment: `environments/camera_lab` (uv, Python 3.12)
- Run records (images + settings + scores): `data/raw_recordings/camera_lab/`
- Calibration results go in `calibration/`, not here (see docs/FileStructure.txt)
