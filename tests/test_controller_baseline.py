"""Offline checks of baseline rejection and measured completion; no hardware imports."""
import sys
from pathlib import Path
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "build"))
import controller_baseline as baseline


class Robot:
    def __init__(self, stalled=False, clipped=False):
        self.value = 10.0
        self.sent = []
        self.stalled = stalled
        self.clipped = clipped

    def get_observation(self):
        return {"shoulder_pan.pos": self.value}

    def send_action(self, action):
        self.sent.append(action)
        if not self.stalled:
            self.value = action["shoulder_pan.pos"]
        return {"shoulder_pan.pos": 10.0} if self.clipped else action


class BaselineTests(unittest.TestCase):
    def run_stream(self, robot, **overrides):
        clock = [0.0]
        params = dict(joint="shoulder_pan", delta=20, lower=0, upper=40,
                      speed=60, timeout=2, emit=lambda *a, **kw: None)
        params.update(overrides)
        def sleep(seconds):
            clock[0] += seconds
        with patch.object(baseline.time, "monotonic", side_effect=lambda: clock[0]), \
             patch.object(baseline.time, "sleep", side_effect=sleep):
            return baseline.move_streamed(robot, **params)

    def test_streamed_visible_move_respects_speed_and_measures_result(self):
        robot = Robot()
        result = self.run_stream(robot)
        self.assertAlmostEqual(result["measured"], 30)
        targets = [a["shoulder_pan.pos"] for a in robot.sent]
        self.assertTrue(all(0 <= b-a <= 60/30 + 1e-6 for a,b in zip(targets,targets[1:])))

    def test_streamed_stall_is_failure(self):
        with self.assertRaises(TimeoutError):
            self.run_stream(Robot(stalled=True))

    def test_streamed_invalid_target_has_no_writes(self):
        robot = Robot()
        with self.assertRaises(ValueError):
            self.run_stream(robot, upper=25)
        self.assertEqual(robot.sent, [])

    def test_startup_requires_torque_off_before_any_target(self):
        from unittest.mock import Mock
        robot = Mock()
        robot.bus.sync_read.return_value = {joint: 1 for joint in baseline.JOINTS}
        with self.assertRaises(RuntimeError):
            baseline.prepare_startup_targets(robot)
        robot.send_action.assert_not_called()

    def test_startup_replaces_old_targets_with_measured_pose(self):
        from unittest.mock import Mock
        robot = Mock()
        robot.bus.sync_read.return_value = {joint: 0 for joint in baseline.JOINTS}
        measured = {joint + ".pos": float(i) for i, joint in enumerate(baseline.JOINTS)}
        robot.get_observation.return_value = measured
        robot.send_action.side_effect = lambda action: action
        with patch("builtins.print"):
            baseline.prepare_startup_targets(robot)
        robot.send_action.assert_called_once_with(measured)

    def run_move(self, robot, **overrides):
        params = dict(joint="shoulder_pan", delta=1.0, lower=8.0, upper=12.0,
                      tolerance=0.1, duration=0.2, timeout=1.0, emit=lambda _: None)
        params.update(overrides)
        clock = [0.0]
        def sleep(seconds):
            clock[0] += seconds
        with patch.object(baseline.time, "monotonic", side_effect=lambda: clock[0]), \
             patch.object(baseline.time, "sleep", side_effect=sleep):
            return baseline.move_joint(robot, **params)

    def test_measured_completion_and_single_joint_only(self):
        robot = Robot()
        result = self.run_move(robot)
        self.assertAlmostEqual(result["measured"], 11.0)
        self.assertTrue(all(set(a) == {"shoulder_pan.pos"} for a in robot.sent))
        self.assertTrue(all(10 <= a["shoulder_pan.pos"] <= 11 for a in robot.sent))

    def test_rejects_target_before_any_write(self):
        robot = Robot()
        with self.assertRaises(ValueError):
            self.run_move(robot, upper=10.5)
        self.assertEqual(robot.sent, [])

    def test_acknowledged_writes_are_not_completion(self):
        with self.assertRaises(TimeoutError):
            self.run_move(Robot(stalled=True))

    def test_controller_clipping_is_not_success(self):
        with self.assertRaises(RuntimeError):
            self.run_move(Robot(clipped=True))

    def test_invalid_feedback_never_written(self):
        robot = Robot()
        robot.value = float("nan")
        with self.assertRaises(ValueError):
            self.run_move(robot)
        self.assertEqual(robot.sent, [])

    def test_interruption_propagates_without_more_writes(self):
        robot = Robot()
        with patch.object(baseline.time, "sleep", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                baseline.move_joint(robot, joint="shoulder_pan", delta=1, lower=8, upper=12,
                                    tolerance=.1, duration=1, timeout=2, emit=lambda _: None)
        self.assertEqual(len(robot.sent), 1)
