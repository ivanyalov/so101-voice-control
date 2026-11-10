import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest

spec = importlib.util.spec_from_file_location("hold", Path(__file__).resolve().parents[1] / "build/cyberwave_hold.py")
hold = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hold)


class Bus:
    def __init__(self):
        self.events = []
        self.regs = {"Homing_Offset": {"joint": 100}, "Min_Position_Limit": {"joint": 10},
                     "Max_Position_Limit": {"joint": 4000}, "Operating_Mode": {"joint": 0},
                     "Torque_Enable": {"joint": 0}, "Present_Position": {"joint": 1700},
                     "Goal_Position": {"joint": 3200}}

    def connect(self):
        self.events.append("connect")

    def disconnect(self):
        self.events.append("close")

    def sync_read(self, register, **kwargs):
        return self.regs[register].copy()

    def sync_write(self, register, positions, **kwargs):
        self.events.append((register, positions.copy()))
        self.regs[register] = positions.copy()

    def enable_torque(self):
        self.events.append("enable")
        self.regs["Torque_Enable"] = {"joint": 1}


def follower():
    bus = Bus()
    return SimpleNamespace(bus=bus, connected=False, _connected=False, _torque_enabled=False,
                           motors={"joint": SimpleNamespace(id=1)},
                           calibration={"joint": SimpleNamespace(id=1, homing_offset=100, range_min=10, range_max=4000)},
                           get_observation=lambda: {"joint.pos": 0})


class HoldTests(unittest.TestCase):
    def test_stale_goal_replaced_before_torque(self):
        f = follower()
        hold.connect_preserving_pose(f)
        self.assertEqual(f.bus.events, ["connect", ("Goal_Position", {"joint": 1700}), "enable"])

    def test_mismatched_calibration_never_writes_or_enables(self):
        f = follower()
        f.bus.regs["Homing_Offset"]["joint"] = 0
        with self.assertRaisesRegex(RuntimeError, "Calibration mismatch"):
            hold.connect_preserving_pose(f)
        self.assertEqual(f.bus.events, ["connect", "close"])

    def test_incomplete_read_never_enables(self):
        f = follower()
        f.bus.regs["Present_Position"] = {}
        with self.assertRaisesRegex(RuntimeError, "Incomplete motor read"):
            hold.connect_preserving_pose(f)
        self.assertEqual(f.bus.events, ["connect", "close"])

    def test_disconnect_holds_and_keeps_torque(self):
        f = follower()
        f.connected = True
        f._torque_enabled = True
        hold.disconnect_preserving_pose(f)
        self.assertEqual(f.bus.events, [("Goal_Position", {"joint": 1700}), "close"])


if __name__ == "__main__":
    unittest.main()
