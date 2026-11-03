"""Set the ID + baudrate of ONE connected SO-101 motor (uses lerobot's own code path).

    python set_motor_id.py COM9 gripper

Why not `lerobot-setup-motors`: that tool always walks the full gripper -> shoulder_pan
sequence and waits for Enter between motors. When you only have one motor wired up,
it would happily assign the *next* ID to that same motor. This script sets exactly
one joint and stops.

Motor table mirrors lerobot's SO101Follower / SO101Leader (identical IDs on both arms).
"""

import sys

from lerobot.motors import Motor, MotorNormMode
from lerobot.motors.feetech import FeetechMotorsBus

MOTORS = {
    "shoulder_pan": Motor(1, "sts3215", MotorNormMode.DEGREES),
    "shoulder_lift": Motor(2, "sts3215", MotorNormMode.DEGREES),
    "elbow_flex": Motor(3, "sts3215", MotorNormMode.DEGREES),
    "wrist_flex": Motor(4, "sts3215", MotorNormMode.DEGREES),
    "wrist_roll": Motor(5, "sts3215", MotorNormMode.DEGREES),
    "gripper": Motor(6, "sts3215", MotorNormMode.RANGE_0_100),
}


def main() -> None:
    if len(sys.argv) != 3 or sys.argv[2] not in MOTORS:
        print(__doc__)
        print("joints:", ", ".join(MOTORS))
        raise SystemExit(2)

    port, joint = sys.argv[1], sys.argv[2]
    bus = FeetechMotorsBus(port=port, motors={joint: MOTORS[joint]})
    bus.setup_motor(joint)  # finds the single motor, disables torque, writes ID + baudrate
    print(f"[{port}] '{joint}' -> id {MOTORS[joint].id}, baud {bus.default_baudrate}")
    bus.disconnect()


if __name__ == "__main__":
    main()
