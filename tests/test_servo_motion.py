"""Check the bounded diagnostic packet without opening any serial ports."""

from pathlib import Path
import sys
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "build"))
from servo_motion import motion_bytes, write_profile


class ServoMotionTests(unittest.TestCase):
    def test_atomic_packet_contains_acceleration_target_time_and_speed(self):
        self.assertEqual(motion_bytes(2326), [1, 22, 9, 0, 0, 57, 0])

    def test_invalid_targets_are_rejected_before_write(self):
        packet = Mock()
        for target in (-1, 4096, 1.5, True):
            with self.assertRaises(ValueError):
                write_profile(packet, object(), 1, target)
        packet.writeTxRx.assert_not_called()

    def test_zero_or_excessive_profile_values_are_rejected(self):
        for args in ({"speed": 0}, {"speed": 115}, {"acceleration": 0}, {"acceleration": 11}):
            with self.assertRaises(ValueError):
                motion_bytes(2000, **args)

    def test_profile_uses_only_one_sram_write(self):
        packet = Mock()
        packet.writeTxRx.return_value = (0, 0)
        port = object()
        write_profile(packet, port, 6, 2048)
        packet.writeTxRx.assert_called_once_with(port, 6, 41, 7, [1, 0, 8, 0, 0, 57, 0])
        packet.write1ByteTxRx.assert_not_called()
        packet.write2ByteTxRx.assert_not_called()

    def test_missing_ack_or_device_error_is_not_treated_as_success(self):
        for result in ((-6, 0), (0, 1)):
            packet = Mock()
            packet.writeTxRx.return_value = result
            with self.assertRaises(RuntimeError):
                write_profile(packet, object(), 1, 2000)


if __name__ == "__main__":
    unittest.main()
