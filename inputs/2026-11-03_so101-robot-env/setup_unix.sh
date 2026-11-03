#!/usr/bin/env bash
# Build the SO-101 robot-side environment on macOS / Linux.
# Requires Python 3.11 or 3.12.
set -e
cd "$(dirname "$0")"

python3 --version
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements-lock.txt

CAL="$HOME/.cache/huggingface/lerobot/calibration"
mkdir -p "$CAL/robots/so_follower" "$CAL/teleoperators/so_leader"
cp calibration/so101_follower.json "$CAL/robots/so_follower/"
cp calibration/so101_leader.json "$CAL/teleoperators/so_leader/"

echo
echo "============================================"
echo " Done. Next:"
echo "   source .venv/bin/activate"
echo "   python robot/scan_motors.py /dev/ttyACM0"
echo "   python robot/drive_arm_demo.py /dev/ttyACM0"
echo "============================================"
