"""Safe follower test from a hand-folded pose using documented SO-101 ranges.

Sources:
  - Joint limits: Simulation/SO101/so101_new_calib.xml (new calib = zero at mid-range)
  - Demo / rest pose + units: team so101-robot-env README
  - Speed: SPEED_TUNING.md (30 Hz smoothstep stream)
"""

from __future__ import annotations

import logging
import math
import shutil
import sys
import time
from pathlib import Path

logging.getLogger().setLevel(logging.ERROR)

from lerobot.robots.so_follower import SO101Follower, SO101FollowerConfig

ROOT = Path(__file__).resolve().parents[1]
TEAM_CALIB = ROOT / "inputs/2026-11-03_so101-robot-env/calibration/so101_follower.json"
HF_CALIB = Path.home() / ".cache/huggingface/lerobot/calibration/robots/so_follower/so101_follower.json"

PORT = sys.argv[1] if len(sys.argv) > 1 else "/dev/cu.usbmodem5B910262011"
ROBOT_ID = "so101_follower"
JOINTS = ["shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll", "gripper"]

# Documented MuJoCo new-calib limits (rad -> deg). Gripper is LeRobot 0..100.
DOC_LIMITS = {
    "shoulder_pan": (-math.degrees(1.91986), math.degrees(1.91986)),   # ±110°
    "shoulder_lift": (-math.degrees(1.74533), math.degrees(1.74533)),  # ±100°
    "elbow_flex": (-math.degrees(1.69), math.degrees(1.69)),           # ±96.8°
    "wrist_flex": (-math.degrees(1.65806), math.degrees(1.65806)),     # ±95°
    "wrist_roll": (-math.degrees(2.74385), math.degrees(2.84121)),     # ~-157..+163°
    "gripper": (0.0, 100.0),
}

# Per-joint travel from the pose you folded by hand (kept inside DOC_LIMITS).
TEST_DELTA = {
    "shoulder_pan": 25.0,
    "shoulder_lift": 20.0,
    "elbow_flex": 20.0,
    "wrist_flex": 25.0,
    "wrist_roll": 25.0,
    "gripper": 30.0,
}

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
ARRIVAL_TOL = {"gripper": 8.0}  # degrees / gripper units
DEFAULT_TOL = 6.0
MARGIN = 5.0  # keep this many units inside doc limits


def ensure_calib() -> None:
    HF_CALIB.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(TEAM_CALIB, HF_CALIB)


def read_pose(robot: SO101Follower) -> dict[str, float]:
    obs = robot.get_observation()
    return {j: float(obs[f"{j}.pos"]) for j in JOINTS}


def clamp_doc(joint: str, value: float) -> float:
    lo, hi = DOC_LIMITS[joint]
    return max(lo + MARGIN, min(hi - MARGIN, value))


def move_to(robot: SO101Follower, target: dict[str, float]) -> dict[str, float]:
    start = read_pose(robot)
    goal = {j: clamp_doc(j, target.get(j, start[j])) for j in JOINTS}
    delta = {j: goal[j] - start[j] for j in JOINTS}
    duration = max(max(abs(delta[j]) / SPEED[j] for j in JOINTS), 2 * ACCEL_TIME)
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

    # Allow slow joints: budget as if the arm only crawled at ~12 units/s.
    max_travel = max(abs(delta[j]) for j in JOINTS)
    deadline = time.perf_counter() + max(12.0, duration + 4.0, max_travel / 12.0 + 6.0)
    while time.perf_counter() < deadline:
        pose = read_pose(robot)
        if all(abs(pose[j] - goal[j]) <= ARRIVAL_TOL.get(j, DEFAULT_TOL) for j in JOINTS):
            return pose
        robot.send_action({f"{j}.pos": goal[j] for j in JOINTS})
        time.sleep(1.0 / HZ)
    pose = read_pose(robot)
    bad = {
        j: (round(pose[j], 1), round(goal[j], 1))
        for j in JOINTS
        if abs(pose[j] - goal[j]) > ARRIVAL_TOL.get(j, DEFAULT_TOL)
    }
    if bad:
        raise RuntimeError(f"Did not reach target: {bad}")
    return pose


def main() -> None:
    ensure_calib()
    import builtins

    builtins.input = lambda *a, **k: ""

    robot = SO101Follower(
        SO101FollowerConfig(port=PORT, id=ROBOT_ID, max_relative_target=15.0)
    )
    robot.connect()
    try:
        robot.bus.write_calibration(robot.calibration)
        print("Acceleration", robot.bus.sync_read("Acceleration"))
        pose = read_pose(robot)
        print("current (folded by hand):")
        for j in JOINTS:
            print(f"  {j:14} {pose[j]:7.1f}   doc {DOC_LIMITS[j][0]:7.1f}..{DOC_LIMITS[j][1]:7.1f}")

        # Home = your fold, pulled just inside documented limits.
        home = {j: clamp_doc(j, pose[j]) for j in JOINTS}
        if any(abs(home[j] - pose[j]) > 1.0 for j in JOINTS):
            print("\n→ pulling slightly inside doc limits")
            move_to(robot, home)
        print("home:", {j: round(v, 1) for j, v in read_pose(robot).items()})

        for joint in JOINTS:
            delta = TEST_DELTA[joint]
            low = clamp_doc(joint, home[joint] - delta)
            high = clamp_doc(joint, home[joint] + delta)
            print(f"\n=== {joint}: home → {low:.0f} → {high:.0f} → home ===")
            move_to(robot, {**home, joint: low})
            move_to(robot, {**home, joint: high})
            move_to(robot, home)
            print(f"{joint}: ok")

        print("\nPASS — doc-range test complete")
    finally:
        robot.disconnect()
        print("disconnected (torque off)")


if __name__ == "__main__":
    main()
