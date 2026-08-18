#!/usr/bin/env bash
# Pin the scene camera's exposure and white balance.
#
# V4L2 controls reset when the camera is replugged or the machine reboots, and
# the camera's auto mode meters on the blown-out wall -- which washes the yellow
# basket to pure white and makes it indistinguishable from the background.
# Run this before every recording session.
#
#   ./rig_tools/cam_setup.sh                 # defaults below
#   ./rig_tools/cam_setup.sh /dev/video4 3000
#
# Measured on this rig (yellow basket saturation / fraction of blown pixels):
#   auto      21.5 sat, 87% blown   <- yellow reads as white, unusable
#   exp 3000 138.1 sat, 23% blown   <- current setting
#   exp 2500 162.8 sat,  0% blown   <- more colour, darker table
# Lower exposure keeps more colour but darkens the blocks on the black mat.
# If blocks stop standing out, raise it; if yellow washes out, lower it.

set -euo pipefail

CAM="${1:-/dev/video4}"
EXPOSURE="${2:-3000}"
WB_TEMP="${3:-4600}"

if ! command -v v4l2-ctl >/dev/null; then
    echo "v4l2-ctl not found. Install it with: sudo apt install v4l-utils" >&2
    exit 1
fi

if [ ! -e "$CAM" ]; then
    echo "$CAM does not exist. Available:" >&2
    ls /dev/video* >&2
    exit 1
fi

# auto_exposure: 1 = manual, 3 = aperture priority (auto)
v4l2-ctl -d "$CAM" \
    --set-ctrl=auto_exposure=1 \
    --set-ctrl=exposure_time_absolute="$EXPOSURE" \
    --set-ctrl=white_balance_automatic=0 \
    --set-ctrl=white_balance_temperature="$WB_TEMP"

echo "$CAM: exposure=$EXPOSURE (manual), white balance=$WB_TEMP (manual)"
v4l2-ctl -d "$CAM" --get-ctrl=auto_exposure,exposure_time_absolute,white_balance_automatic,white_balance_temperature
