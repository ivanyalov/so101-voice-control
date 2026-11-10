"""Supervised, single-joint baseline through Ivy's LeRobot API.

No device is opened without --live and a terminal confirmation. Numeric bounds
are supplied by the operator; they are not inferred from calibration files.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import time

JOINTS = ("shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll", "gripper")


def ports():
    from serial.tools.list_ports import comports
    return [{"device": p.device, "serial": p.serial_number, "description": p.description}
            for p in comports()]


def validate(args):
    numbers = (args.delta, args.lower, args.upper, args.tolerance, args.duration, args.timeout)
    if not all(math.isfinite(n) for n in numbers):
        raise ValueError("All motion parameters must be finite")
    if not 0 < abs(args.delta) <= 2:
        raise ValueError("Baseline delta must be nonzero and at most 2 interface units")
    if args.lower >= args.upper or not 0 < args.tolerance < abs(args.delta):
        raise ValueError("Need lower < upper and 0 < tolerance < abs(delta)")
    if args.duration <= 0 or args.timeout <= args.duration:
        raise ValueError("Need 0 < duration < timeout")
    if not args.calibration.is_file():
        raise ValueError("An existing calibration file for this physical arm is required")
    if not any(p["device"] == args.port and p["serial"] == args.serial for p in ports()):
        raise ValueError("Port and board serial do not match a connected device")


def position(robot, key):
    value = robot.get_observation()[key]
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
        raise ValueError("Invalid measured joint position")
    return value


def move_joint(robot, *, joint, delta, lower, upper, tolerance, duration, timeout, emit=print):
    """One bounded ramp; success requires measured feedback, never just a write."""
    key = joint + ".pos"
    start = position(robot, key)
    target = start + delta
    if not lower <= start <= upper or not lower <= target <= upper:
        raise ValueError("Measured start or requested target exceeds supplied bounds; no target sent")
    emit(json.dumps({"event": "movement_started", "joint": joint, "start": start, "target": target}))
    began = time.monotonic()
    consecutive = 0
    while True:
        elapsed = time.monotonic() - began
        if elapsed >= timeout:
            raise TimeoutError("Target not confirmed before timeout; movement is not complete")
        measured = position(robot, key)
        if not lower <= measured <= upper:
            raise ValueError("Measured joint left the supplied bounds")
        progress = min(1.0, elapsed / duration)
        requested = start + delta * progress
        sent = robot.send_action({key: requested})
        if key not in sent or not math.isfinite(sent[key]) or abs(sent[key] - requested) > 1e-6:
            raise RuntimeError("Controller changed the requested target; stopping this check")
        consecutive = consecutive + 1 if progress == 1 and abs(measured - target) <= tolerance else 0
        if consecutive >= 3:
            result = {"event": "measured_completion", "joint": joint, "start": start,
                      "target": target, "measured": measured, "elapsed_seconds": elapsed}
            emit(json.dumps(result))
            return result
        time.sleep(0.05)


def hold(robot, joint):
    """Best effort only: a communication failure can make a software hold impossible."""
    key = joint + ".pos"
    robot.send_action({key: position(robot, key)})


def move_streamed(robot, *, joint, delta, lower, upper, speed, hz=30.0,
                  accel_time=0.25, tolerance=0.8, timeout=5.0, emit=print):
    """Speed-limited smoothstep from SPEED_TUNING.md, with measured completion.

    Smoothstep's peak derivative is 1.5, so duration includes that factor to
    respect the requested peak speed. Clipped writes are allowed and followed
    by fresh readback; a write acknowledgement is never completion.
    """
    values = (delta, lower, upper, speed, hz, accel_time, tolerance, timeout)
    if not all(math.isfinite(value) for value in values):
        raise ValueError("Non-finite movement parameter")
    if not 0 < abs(delta) <= 30 or lower >= upper:
        raise ValueError("Initial streamed test requires a nonzero delta of at most 30 units and valid bounds")
    if not 0 < tolerance < abs(delta) or min(speed, hz, accel_time, timeout) <= 0:
        raise ValueError("Invalid timing, speed or tolerance")
    duration = max(1.5 * abs(delta) / speed, 2 * accel_time)
    if timeout <= duration:
        raise ValueError("Timeout must exceed the planned ramp")
    key = joint + ".pos"
    start = position(robot, key)
    target = start + delta
    if not lower <= start <= upper or not lower <= target <= upper:
        raise ValueError("Start or target outside supplied bounds; no movement sent")
    emit(json.dumps({"event": "movement_started", "joint": joint, "start": start,
                     "target": target, "speed": speed, "hz": hz, "ramp_seconds": duration}), flush=True)
    began = time.monotonic()
    deadline = began
    consecutive = 0
    samples = []
    while True:
        elapsed = time.monotonic() - began
        if elapsed >= timeout:
            raise TimeoutError("Streamed target not reached before timeout")
        measured = position(robot, key)
        samples.append({"elapsed": elapsed, "position": measured})
        if not lower <= measured <= upper:
            raise ValueError("Measured position left supplied bounds")
        u = min(1.0, elapsed / duration)
        requested = start + delta * u * u * (3 - 2 * u)
        sent = robot.send_action({key: requested})
        if key not in sent or not math.isfinite(sent[key]) or not lower <= sent[key] <= upper:
            raise RuntimeError("Invalid controller target acknowledgement")
        consecutive = consecutive + 1 if u == 1 and abs(measured - target) <= tolerance else 0
        if consecutive >= 3:
            result = {"event": "measured_completion", "joint": joint, "start": start,
                      "target": target, "measured": measured, "elapsed_seconds": elapsed,
                      "average_measured_degrees_per_second": abs(measured-start)/elapsed,
                      "samples": samples}
            emit(json.dumps(result), flush=True)
            return result
        deadline = max(deadline + 1 / hz, time.monotonic())
        time.sleep(max(0.0, deadline - time.monotonic()))


def prepare_startup_targets(robot):
    """Replace stale goals while torque is off, before standard configuration enables it."""
    torque = robot.bus.sync_read("Torque_Enable", normalize=False)
    if set(torque) != set(JOINTS) or any(value != 0 for value in torque.values()):
        raise RuntimeError("Baseline startup requires all six motors torque-off; no goals changed")
    measured = {joint + ".pos": position(robot, joint + ".pos") for joint in JOINTS}
    sent = robot.send_action(measured)
    if set(sent) != set(measured) or any(abs(sent[key] - value) > 1e-6 for key, value in measured.items()):
        raise RuntimeError("Startup hold targets were changed by the controller; torque remains off")
    print(json.dumps({"event": "startup_targets_primed", "measured_pose": measured}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("ports", help="List serial devices; do not connect to the arm")
    jog = sub.add_parser("jog", help="Review or execute one small measured relative movement")
    jog.add_argument("--port", required=True)
    jog.add_argument("--serial", required=True)
    jog.add_argument("--calibration", type=Path, required=True)
    jog.add_argument("--joint", choices=JOINTS, required=True)
    for name in ("delta", "lower", "upper", "tolerance", "duration", "timeout"):
        jog.add_argument("--" + name, type=float, required=True)
    jog.add_argument("--live", action="store_true")
    args = parser.parse_args()
    if args.command == "ports":
        print(json.dumps(ports(), indent=2))
        return 0
    validate(args)
    units = "0-100 gripper units" if args.joint == "gripper" else "degrees"
    print(f"{'LIVE' if args.live else 'PREVIEW — movement disabled'}: {args.joint}, delta {args.delta}, {units}")
    print("Bounds and timing are operator-supplied test parameters, not verified robot limits.")
    if not args.live:
        print("No connection opened. Add --live only for the supervised baseline.")
        return 0
    print("Connection runs LeRobot's normal motor configuration. Recalibration is blocked.")
    print("Ctrl+C cancels the ramp and attempts to hold the measured joint position.")
    print("Exit preserves torque; it does not release the arm. Keep physical power access available.")
    if input("Arm secured, powered, workspace clear, operator present? Type READY: ").strip() != "READY":
        print("Cancelled before connection")
        return 1
    from lerobot.robots.so_follower import SO101Follower, SO101FollowerConfig

    class ExistingCalibrationFollower(SO101Follower):
        def calibrate(self):
            raise RuntimeError("Calibration missing or mismatched. Stopped without recalibrating.")

        def configure(self):
            prepare_startup_targets(self)
            super().configure()

    robot = ExistingCalibrationFollower(SO101FollowerConfig(
        port=args.port, id=args.calibration.stem, calibration_dir=args.calibration.parent,
        use_degrees=True, max_relative_target=abs(args.delta), disable_torque_on_disconnect=False))
    try:
        robot.connect()
        move_joint(robot, joint=args.joint, delta=args.delta, lower=args.lower, upper=args.upper,
                   tolerance=args.tolerance, duration=args.duration, timeout=args.timeout)
        return 0
    except BaseException:
        if robot.is_connected:
            try:
                hold(robot, args.joint)
                print("Requested hold at measured position; physical stop is not independently verified.")
            except Exception:
                print("HOLD FAILED. Use physical power control if needed.")
        raise
    finally:
        if robot.bus.is_connected:
            robot.bus.disconnect(disable_torque=False)
            print("Serial connection closed; torque was not intentionally disabled.")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Interrupted; no further sequence steps will run.")
        raise SystemExit(130)
    except Exception as exc:
        print(f"Baseline failed: {exc}")
        raise SystemExit(1)
