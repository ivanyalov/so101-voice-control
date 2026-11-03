"""Move each follower joint 15 degrees inward and back, one at a time."""

import time

from robot_control import RobotBus, load_config
from robot_probe import JOINTS
from servo_motion import write_profile


def main():
    bus = RobotBus(load_config())
    try:
        bus.connect(("follower",))
        arm = bus.snapshot()["follower"]
        plan = []
        for joint in JOINTS:
            item = arm[joint]
            if item["issues"]:
                raise RuntimeError("; ".join(item["issues"]))
            cal, state = item["calibration"], item["state"]
            low, high = cal["range_min"] + 8, cal["range_max"] - 8
            measured = state["present_position"]
            home = min(high, max(low, measured))
            if abs(home - measured) > 24:
                raise RuntimeError(f"{joint}: current position is outside the permitted starting range")
            candidates = [home + d for d in (171, -171) if low <= home + d <= high]
            if not candidates:
                raise RuntimeError(f"{joint}: no room for a 15-degree movement")
            target = min(candidates, key=lambda p: abs(p - (low + high) / 2))
            plan.append((joint, cal, home, target))

        def move(joint, cal, target):
            motor_id = cal["id"]
            bus.touched[("follower", motor_id)] = cal
            write_profile(bus.packet, bus.ports["follower"], motor_id, target)
            bus.write("follower", motor_id, "torque", 1)
            deadline, stable = time.monotonic() + 7, 0
            while time.monotonic() < deadline:
                pos = bus.position("follower", motor_id)
                if not cal["range_min"] - 12 <= pos <= cal["range_max"] + 12:
                    raise RuntimeError(f"{joint}: unexpected position")
                if bus.read("follower", motor_id, 65, 1):
                    raise RuntimeError(f"{joint}: motor fault")
                stable = stable + 1 if abs(pos - target) <= 12 else 0
                if stable >= 3:
                    return
                time.sleep(0.05)
            raise RuntimeError(f"{joint}: target not reached; stopping")

        for joint, cal, home, target in plan:
            print(f"{joint}: 15 degrees out and back", flush=True)
            move(joint, cal, home)
            move(joint, cal, target)
            time.sleep(0.25)
            move(joint, cal, home)
            time.sleep(0.25)
        print("Done. Follower holds the return pose.", flush=True)
    except BaseException:
        for error in bus.hold():
            print("Hold could not be confirmed: " + error, flush=True)
        raise
    finally:
        bus.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Stopped.")
    except Exception as exc:
        raise SystemExit(str(exc))
