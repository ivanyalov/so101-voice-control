"""Full-range follower sweep using team calibration + LeRobot streaming.

Uses SPEED_TUNING defaults (30 Hz, smoothstep), not the old Feetech jog path.
"""

from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path

from lerobot.motors.motors_bus import MotorNormMode
from lerobot.robots.so_follower import SO101Follower, SO101FollowerConfig

ROOT = Path(__file__).resolve().parents[1]
TEAM_CALIB = ROOT / "inputs/2026-11-03_so101-robot-env/calibration/so101_follower.json"
HF_CALIB = Path.home() / ".cache/huggingface/lerobot/calibration/robots/so_follower/so101_follower.json"

PORT = sys.argv[1] if len(sys.argv) > 1 else "/dev/cu.usbmodem5B910262011"
ROBOT_ID = "so101_follower"
JOINTS = ["shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll", "gripper"]
MARGIN = 0.06  # stay 6% inside calibrated hardstops
HZ = 30.0
ACCEL_TIME = 0.25
SPEED = {
    "shoulder_pan": 90,
    "shoulder_lift": 70,
    "elbow_flex": 70,
    "wrist_flex": 90,
    "wrist_roll": 120,
    "gripper": 100,
}


def ensure_team_calibration() -> None:
    HF_CALIB.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(TEAM_CALIB, HF_CALIB)


def joint_limits(robot: SO101Follower) -> dict[str, tuple[float, float]]:
    limits = {}
    for name, motor in robot.bus.motors.items():
        cal = robot.calibration[name]
        span = cal.range_max - cal.range_min
        lo = cal.range_min + span * MARGIN
        hi = cal.range_max - span * MARGIN
        if motor.norm_mode is MotorNormMode.DEGREES:
            mid = (cal.range_min + cal.range_max) / 2
            limits[name] = ((lo - mid) * 360 / 4095, (hi - mid) * 360 / 4095)
        elif motor.norm_mode is MotorNormMode.RANGE_0_100:
            limits[name] = (
                ((lo - cal.range_min) / span) * 100,
                ((hi - cal.range_min) / span) * 100,
            )
        else:
            raise RuntimeError(f"Unexpected norm mode for {name}")
    return limits


def read_pose(robot: SO101Follower) -> dict[str, float]:
    obs = robot.get_observation()
    return {j: float(obs[f"{j}.pos"]) for j in JOINTS}


def move_to(robot: SO101Follower, target: dict[str, float], speed: dict[str, float] | None = None) -> dict[str, float]:
    speed = speed or SPEED
    start = read_pose(robot)
    goal = {j: target.get(j, start[j]) for j in JOINTS}
    delta = {j: goal[j] - start[j] for j in JOINTS}
    duration = max(max(abs(delta[j]) / speed[j] for j in JOINTS), 2 * ACCEL_TIME)
    steps = max(1, round(duration * HZ))
    t0 = time.perf_counter()
    for i in range(1, steps + 1):
        u = i / steps
        s = u * u * (3 - 2 * u)
        action = {f"{j}.pos": start[j] + delta[j] * s for j in JOINTS}
        robot.send_action(action)
        wait = t0 + i / HZ - time.perf_counter()
        if wait > 0:
            time.sleep(wait)
    return read_pose(robot)


def main() -> None:
    ensure_team_calibration()
    print(f"Using team calibration: {TEAM_CALIB}")
    print(json.dumps(json.loads(TEAM_CALIB.read_text()), indent=2)[:200], "...")

    # Accept existing calibration file without interactive prompt.
    import builtins

    builtins.input = lambda *args, **kwargs: ""

    robot = SO101Follower(
        SO101FollowerConfig(
            port=PORT,
            id=ROBOT_ID,
            max_relative_target=15.0,
        )
    )
    robot.connect()
    try:
        # Force team file onto the servos in case Codex left something else.
        robot.bus.write_calibration(robot.calibration)
        pose = read_pose(robot)
        limits = joint_limits(robot)
        print("current pose:")
        for j in JOINTS:
            lo, hi = limits[j]
            print(f"  {j:14} {pose[j]:8.1f}   limits [{lo:7.1f}, {hi:7.1f}]")

        print("\ncentering all joints...")
        mid = {j: (limits[j][0] + limits[j][1]) / 2 for j in JOINTS}
        move_to(robot, mid)

        for joint in JOINTS:
            lo, hi = limits[joint]
            print(f"\n=== {joint}: mid -> low -> high -> mid ===")
            move_to(robot, {**mid, joint: lo})
            move_to(robot, {**mid, joint: hi})
            move_to(robot, mid)
            print(f"{joint}: ok")

        print("\nrange sweep complete; holding mid pose 1s")
        time.sleep(1.0)
    finally:
        robot.disconnect()
        print("disconnected (torque released)")


if __name__ == "__main__":
    main()
