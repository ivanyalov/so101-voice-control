import sys
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'build'))
import cyberwave_trajectory as trajectory


class LocalTrajectoryTests(unittest.TestCase):
    def test_absolute_local_schedule_does_not_accumulate_write_latency(self):
        clock = [0.0]
        event = Mock()
        event.wait.side_effect = lambda delay: clock.__setitem__(0, clock[0] + delay) or False
        items = trajectory.schedule_chunks({'local_trajectory_hz': 30}, 150)
        times = []
        with patch.object(trajectory.time, 'monotonic', side_effect=lambda: clock[0]):
            for item in items:
                self.assertTrue(trajectory.wait_for_chunk(item, event))
                times.append(clock[0])
                clock[0] += .005
        for i, stamp in enumerate(times):
            self.assertAlmostEqual(stamp, i / 30)

    def test_stall_aborts_instead_of_bursting_delayed_targets(self):
        event = Mock()
        meta = {'local_clock': {'start': 0.0}, 'local_offset': .033}
        with patch.object(trajectory.time, 'monotonic', return_value=.3):
            with self.assertRaises(RuntimeError):
                trajectory.wait_for_chunk(meta, event)
        event.wait.assert_not_called()

    def test_cancellation_and_legacy_chunks(self):
        self.assertEqual(trajectory.schedule_chunks({}, 2), [{}, {}])
        event = Mock()
        event.wait.return_value = True
        self.assertFalse(trajectory.wait_for_chunk(trajectory.schedule_chunks({'local_trajectory_hz': 30}, 1)[0], event))
        with self.assertRaises(ValueError):
            trajectory.schedule_chunks({'local_trajectory_hz': 10}, 30)


if __name__ == '__main__':
    unittest.main()
