import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "build"))
import readiness


class ReadinessTests(unittest.TestCase):
    def test_port_inventory_does_not_open_serial_transport(self):
        port = SimpleNamespace(device="/dev/mock", description="Mock USB adapter", vid=1, pid=2)
        with patch("serial.tools.list_ports.comports", return_value=[port]), patch("serial.Serial") as serial:
            self.assertEqual(readiness.list_ports()[0]["device"], "/dev/mock")
            serial.assert_not_called()

    def test_template_is_explicitly_incomplete(self):
        result = readiness.profile_report(ROOT / "config/robot-profile.json")
        self.assertFalse(result["configuration_complete"])
        self.assertFalse(result["servo_connection_opened"])
        self.assertIn("driver_version", result["missing_or_invalid"])

    def test_duplicate_motor_ids_are_rejected(self):
        profile = json.loads((ROOT / "config/robot-profile.json").read_text())
        profile["joints"]["gripper"]["id"] = 1
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profile.json"
            path.write_text(json.dumps(profile))
            result = readiness.profile_report(path)
        self.assertIn("duplicate motor IDs", result["missing_or_invalid"])

    def test_nonfinite_joint_bounds_are_rejected(self):
        profile = json.loads((ROOT / "config/robot-profile.json").read_text())
        profile["joints"]["shoulder_pan"].update({"min": float("nan"), "max": 90})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profile.json"
            path.write_text(json.dumps(profile))
            result = readiness.profile_report(path)
        self.assertIn("joints.shoulder_pan.bounds", result["missing_or_invalid"])


if __name__ == "__main__":
    unittest.main()
