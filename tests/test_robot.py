"""Channel safety and measured completion. No network or robot access."""
import math
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'build'))
import robot


class ChannelConfigurationTests(unittest.TestCase):
    def test_dotenv_configures_client_without_local_edge_files(self):
        twin = '36c617e6-4027-4ddc-b815-1557394c673a'
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / '.env').write_text(
                f'CYBERWAVE_TWIN_UUID={twin}\nCYBERWAVE_API_KEY=client-key\n'
                f'CYBERWAVE_WORKSPACE_ID=client-workspace\nCYBERWAVE_EDGE_CONFIG_DIR={root / "absent"}\n'
            )
            sdk = Mock()
            with patch.object(robot, 'ROOT', root), patch.dict(os.environ, {}, clear=True), \
                    patch.dict(sys.modules, {'cyberwave': SimpleNamespace(Cyberwave=sdk)}):
                channel = robot.configured_channel()

        sdk.assert_called_once_with(api_key='client-key', mode='live', workspace_id='client-workspace')
        self.assertEqual(channel.twin, twin)


def frame(**changes):
    return {'source_type': 'edge_follower', 'timestamp': time.time(),
            'positions': {j: 0.0 for j in robot.JOINTS.values()},
            'session_id': 'physical-session', **changes}


class ChannelTests(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        self.client.mqtt.topic_prefix = ''
        self.client.mqtt.connected = True
        self.channel = robot.Channel(self.client, 'test-twin')

    def test_disconnected_never_dispatches(self):
        with self.assertRaisesRegex(RuntimeError, 'no movement sent'):
            self.channel.move('wrist_roll', 5, relative=True, timeout=0.01)
        self.client.mqtt.publish.assert_not_called()

    def test_only_complete_fresh_physical_frames_accepted(self):
        bad = [frame(source_type='sim'), frame(source_type='tele'),
               frame(timestamp=time.time()-100), frame(timestamp=time.time()+20),
               frame(timestamp=True), frame(session_id=''), frame(positions={'_1': 0}),
               frame(positions={j: float('nan') for j in robot.JOINTS.values()})]
        for item in bad:
            self.channel.receive(item)
            self.assertIsNone(self.channel.snapshot())
        self.channel.receive(frame())
        self.assertIsNotNone(self.channel.snapshot())

    def test_out_of_order_frame_cannot_replace_current(self):
        current = frame()
        self.channel.receive(current)
        self.channel.receive(frame(timestamp=current['timestamp']-0.1))
        self.assertEqual(self.channel.snapshot()['timestamp'], current['timestamp'])

    def test_physical_frames_expire(self):
        self.channel.receive(frame())
        with patch.object(robot.time, 'time', return_value=time.time()+10):
            self.assertIsNone(self.channel.snapshot())

    def test_relative_command_uses_radians_and_requires_post_dispatch_feedback(self):
        self.channel.receive(frame(positions={j: math.radians(10) for j in robot.JOINTS.values()}))
        def publish(topic, payload, **options):
            self.assertEqual(options, {'qos': 0})
            self.assertEqual(payload['source_type'], 'tele')
            self.assertEqual(payload['joint_name'], '_5')
            self.assertAlmostEqual(payload['joint_state']['position'], math.radians(15))
            self.channel.receive(frame(timestamp=payload['timestamp']+0.001,
                                       positions={j: math.radians(15) for j in robot.JOINTS.values()}))
            return True
        self.client.mqtt.publish.side_effect = publish
        result = self.channel.move('wrist_roll', 5, relative=True, timeout=0.1)
        self.assertEqual(result['status'], 'measured_within_tolerance')
        self.assertAlmostEqual(result['measured_degrees'], 15)

    def test_local_publish_acceptance_does_not_mean_robot_executed(self):
        self.channel.receive(frame())
        with self.assertRaisesRegex(RuntimeError, 'not confirmed'):
            self.channel.move('elbow_flex', 5, timeout=0.01)
        self.assertEqual(self.client.mqtt.publish.call_count, 1)

    def test_publish_failure_does_not_retry(self):
        self.channel.receive(frame())
        self.client.mqtt.publish.return_value = False
        with self.assertRaisesRegex(RuntimeError, 'rejected'):
            self.channel.move('wrist_flex', 5)
        self.assertEqual(self.client.mqtt.publish.call_count, 1)

    def test_changed_driver_session_is_not_completion(self):
        self.channel.receive(frame())
        def publish(topic, payload, **options):
            self.channel.receive(frame(timestamp=payload['timestamp']+0.001, session_id='restarted'))
            return True
        self.client.mqtt.publish.side_effect = publish
        with self.assertRaisesRegex(RuntimeError, 'session changed'):
            self.channel.move('gripper', 10)

    def test_invalid_coordinates_never_dispatch(self):
        for amount in (float('inf'), float('nan'), True):
            with self.assertRaises(ValueError):
                self.channel.move('shoulder_pan', amount)
        self.client.mqtt.publish.assert_not_called()

    def test_broker_connection_loss_blocks_dispatch(self):
        self.channel.receive(frame())
        self.client.mqtt.connected = False
        with self.assertRaisesRegex(RuntimeError, 'Connection or physical feedback lost'):
            self.channel.move('wrist_roll', 5)
        self.client.mqtt.publish.assert_not_called()

    def test_feedback_loss_after_dispatch_is_not_success(self):
        self.channel.receive(frame())
        def publish(*args, **kwargs):
            self.channel.latest = None
            return True
        self.client.mqtt.publish.side_effect = publish
        with self.assertRaisesRegex(RuntimeError, 'Feedback lost after dispatch'):
            self.channel.move('shoulder_lift', 5)
        self.assertEqual(self.client.mqtt.publish.call_count, 1)

    def test_diagnostic_uses_ping_topic_and_never_joint_target(self):
        handlers = {}
        self.client.mqtt.subscribe.side_effect = lambda topic, handler, **kwargs: handlers.update({topic: handler})
        self.client.mqtt.is_subscribed.return_value = True
        def publish(topic, payload, **kwargs):
            self.assertEqual(topic, 'cyberwave/ping/test-twin/request')
            self.assertEqual(payload['type'], 'ping')
            self.assertNotIn('joint_state', payload)
            handlers[topic](payload)
            return True
        self.client.mqtt.publish.side_effect = publish
        self.assertTrue(self.channel.probe(0.1))
        self.client.mqtt.unsubscribe.assert_called_once()


if __name__ == '__main__':
    unittest.main()
