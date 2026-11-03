"""Minimal example: connect to the SO-101 follower and drive it from Python.

    python drive_arm_demo.py COM9

What it does (safe, no big motions):
  1. connect + load calibration
  2. print the current joint readings
  3. hold the current pose for 2 s
  4. open/close the gripper twice
  5. disconnect (torque released)

This is the shape of the API your middle platform should wrap: every voice command
eventually becomes a dict of {"<joint>.pos": degrees} sent with robot.send_action().
"""

import sys
import time

from lerobot.robots.so_follower import SO101Follower, SO101FollowerConfig

PORT = sys.argv[1] if len(sys.argv) > 1 else "COM9"
ROBOT_ID = "so101_follower"          # must match the calibration file name
JOINTS = ["shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll", "gripper"]


def main() -> None:
    robot = SO101Follower(SO101FollowerConfig(port=PORT, id=ROBOT_ID))
    robot.connect()                   # asks to confirm the calibration file

    try:
        obs = robot.get_observation()
        print("current joint positions (degrees):")
        for j in JOINTS:
            print(f"  {j:14} {obs[f'{j}.pos']:8.1f}")

        print("\nholding current pose for 2s...")
        action = {f"{j}.pos": obs[f"{j}.pos"] for j in JOINTS}
        for _ in range(20):
            robot.send_action(action)
            time.sleep(0.1)

        print("opening / closing gripper twice...")
        for _ in range(2):
            for target in (60.0, 0.0):          # gripper uses 0-100 range
                robot.send_action({**action, "gripper.pos": target})
                time.sleep(0.8)

        print("done.")
    finally:
        robot.disconnect()


if __name__ == "__main__":
    main()
