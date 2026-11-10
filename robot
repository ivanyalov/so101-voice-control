#!/bin/sh
# Works from any current directory; uses this project's existing environment.
ROBOT_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if [ "${1-}" = native ]; then
    shift
    exec "$ROBOT_ROOT/.cache/venv-cyberwave/bin/python" "$ROBOT_ROOT/build/cyberwave_native.py" "$@"
fi
exec "$ROBOT_ROOT/.venv/bin/python" "$ROBOT_ROOT/build/robot.py" "$@"
