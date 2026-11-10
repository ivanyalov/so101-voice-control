"""Saved-pose integrity and coordinated, feedback-gated return without hardware."""
import json
import math
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'build'))
import poses
import robot
import voice_control


class PoseTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'poses.json'
        self.patch = patch.object(poses, 'path', return_value=self.path)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def stored(self, twin='test-twin'):
        return {'schema_version': 1, 'twin': twin, 'poses': {'start': {
            'source': 'edge_follower', 'degrees': {n: 9 for n in robot.JOINTS}}}}

    def test_requires_real_complete_pose_for_matching_twin(self):
        for data in (self.stored('other'), {'schema_version': 1, 'twin': 'test-twin', 'poses': {}},
                     {'schema_version': 1, 'twin': 'test-twin', 'poses': {'start': {'source': 'sim', 'degrees': {}}}}):
            self.path.write_text(json.dumps(data))
            with self.assertRaises(ValueError): poses.load_pose('start', 'test-twin')
        data = self.stored()
        data['poses']['start']['degrees']['gripper'] = float('nan')
        self.path.write_text(json.dumps(data))
        with self.assertRaises(ValueError): poses.load_pose('start', 'test-twin')

    def test_saving_reads_but_never_publishes_or_overwrites(self):
        channel = Mock(twin='test-twin')
        channel.wait_state.return_value = {'timestamp': 123, 'session_id': 'session', 'positions': {j: math.radians(9) for j in robot.JOINTS.values()}}
        poses.save_pose(channel, 'start')
        self.assertAlmostEqual(poses.load_pose('start', 'test-twin')['elbow_flex'], 9)
        channel.client.mqtt.publish.assert_not_called()
        with self.assertRaises(ValueError): poses.save_pose(channel, 'start')

    def test_voice_pose_uses_saved_absolute_angles_without_direction_mapping(self):
        self.path.write_text(json.dumps(self.stored()))
        intent = {'status': 'planned', 'reply': 'Return to start.', 'actions': [{
            'type': 'pose', 'pose': 'start', 'joint': None, 'direction': None, 'amount_degrees': None, 'arm': 'follower'}]}
        with self.assertRaisesRegex(ValueError, 'withheld'):
            voice_control.resolve_actions(intent, {}, 'test-twin')

    def test_pose_api_refuses_before_feedback_or_publish(self):
        client = Mock()
        channel = robot.Channel(client, 'test-twin')
        with self.assertRaisesRegex(RuntimeError, 'withheld'):
            channel.move_pose({n:9 for n in robot.JOINTS})
        client.mqtt.publish.assert_not_called()

    def test_pose_cli_refuses_before_channel_creation(self):
        with patch.object(sys, 'argv', ['robot', 'pose', 'start']), \
             patch.object(robot, 'configured_channel') as factory, \
             patch('sys.stdout'):
            self.assertEqual(robot.main(), 1)
        factory.assert_not_called()


if __name__ == '__main__':
    unittest.main()
