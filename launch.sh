#!/usr/bin/env bash
# Start the camera_lab desktop GUI (Linux equivalent of a launch.bat).
# Usage: ./launch.sh          real cameras
#        ./launch.sh --sim    simulated camera
set -euo pipefail
exec /mnt/workspaces/robotic_cell/environments/camera_lab/bin/camera-lab gui "$@"
