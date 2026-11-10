"""Intent-to-MQTT integration with synthetic observations, without hardware/API."""
import contextlib
import io
import math
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'build'))
import robot
import voice_control as vc
import voice_toggle
from coordinated_fakes import Rig


def action(joint='shoulder_pan', direction='left', amount=45, kind='relative', arm='follower'):
    return {'type': kind, 'joint': joint, 'direction': direction, 'amount_degrees': amount, 'pose': None, 'arm': arm}


def intent(actions=None):
    return {'status': 'planned', 'reply': 'Rotate base left 45 degrees.', 'actions': actions or [action()]}


def profile():
    # Synthetic test signs only; never written to the live profile.
    return {'twin': 'test-twin', 'joints': {
        'shoulder_pan': {'verified': True, 'viewpoint': 'test viewpoint', 'signs': {'left': -1, 'right': 1}}}}


class VoiceControlTests(unittest.TestCase):
    def test_signed_three_joint_sequence_needs_no_physical_mapping(self):
        commands = vc.resolve_actions(intent([
            action('shoulder_pan', 'positive', 20),
            action('wrist_roll', 'positive', 20),
            action('elbow_flex', 'negative', 10),
        ]), {}, 'test-twin')
        self.assertEqual([(c['joint'], c['delta_degrees']) for c in commands],
                         [('shoulder_pan', 20), ('wrist_roll', 20), ('elbow_flex', -10)])

    def setUp(self):
        self.environment = patch.dict(os.environ, {'CYBERWAVE_TWIN_UUID': 'test-twin'})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_base_left_45_resolves_exact_joint_amount_and_sign(self):
        actual = vc.resolve_actions(intent(), profile(), 'test-twin')
        self.assertEqual(actual[0]['joint'], 'shoulder_pan')
        self.assertEqual(actual[0]['delta_degrees'], -45)

    def test_missing_unverified_or_wrong_twin_mapping_never_connects(self):
        unverified = profile()
        unverified['joints']['shoulder_pan']['verified'] = False
        wrong = profile()
        wrong['twin'] = 'other'
        for mapping in ({}, unverified, wrong):
            factory = Mock()
            result = vc.execute_intent(intent(), profile=mapping, channel_factory=factory)
            self.assertEqual(result['status'], 'blocked')
            self.assertEqual(result['executed_actions'], 0)
            factory.assert_not_called()

    def test_entire_sequence_checked_before_first_move(self):
        factory = Mock()
        result = vc.execute_intent(intent([action(), action('wrist_roll', 'clockwise', 30)]), profile=profile(), channel_factory=factory)
        self.assertEqual(result['status'], 'blocked')
        factory.assert_not_called()

    def test_endpoint_jog_and_leader_never_substitute_a_move(self):
        for a in (action(kind='endpoint', amount=None), action(kind='jog', amount=None), action(arm='leader'), action(arm='both')):
            with self.assertRaises(ValueError):
                vc.resolve_actions(intent([a]), profile(), 'test-twin')

    def test_clarification_and_cancel_do_not_connect(self):
        factory = Mock()
        result = vc.execute_intent({'status': 'clarify', 'reply': 'Which way?', 'actions': []}, channel_factory=factory, profile={})
        self.assertEqual(result['status'], 'clarify')
        cancelled = threading.Event()
        cancelled.set()
        self.assertEqual(vc.execute_intent(intent(), profile=profile(), cancelled=cancelled, channel_factory=factory)['status'], 'cancelled')
        factory.assert_not_called()

    def make_channel(self):
        rig = Rig()
        rig.norm['shoulder_pan'] = rig.from_rad(math.radians(10),rig.modes['shoulder_pan'],rig.follower.calibration['shoulder_pan'])
        rig.channel.test_rig = rig
        return rig.channel

    def test_real_converter_to_real_channel_to_expected_mqtt_payload(self):
        channel = self.make_channel()
        with channel.test_rig.active():
            result = vc.execute_intent(intent(), profile=profile(), channel_factory=lambda: channel)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['executed_actions'], 1)
        payload = next(p for _,p in channel.test_rig.sent if p.get('kind')=='segment')
        self.assertEqual(payload['command']['joints']['_1'],dict(type='relative',value=math.radians(-45),unit='radians'))
        self.assertAlmostEqual(result['results'][0]['target_coordinates']['_1'],math.radians(-35))
        channel.client.disconnect.assert_called_once()

    def test_failed_first_motion_prevents_later_sequence_dispatch(self):
        channel = self.make_channel()
        channel.client.mqtt.publish.side_effect = None
        channel.client.mqtt.publish.return_value = False
        with channel.test_rig.active():
            result = vc.execute_intent(intent([action(), action(direction='right')]), profile=profile(), channel_factory=lambda: channel)
        self.assertEqual(result['status'], 'execution_failed')
        self.assertEqual(result['completed_actions'], 0)
        self.assertEqual([c.args[1]['command'] for c in channel.client.mqtt.publish.call_args_list],['remoteoperate','stop'])

    def test_spoken_transcript_runs_same_converter_and_mqtt_channel(self):
        channel = self.make_channel()
        recorder = Mock(recording=True)
        recorder.stop.return_value = Path('synthetic.wav')
        processor = lambda text: vc.execute_intent(vc.grok_planner.interpret(text), profile=profile(), channel_factory=lambda: channel)
        session = voice_toggle.Session(recorder, processor=processor)
        with patch.object(voice_toggle.readiness, 'transcribe', return_value={'text': 'base to the left 45'}), \
             patch.object(vc.grok_planner, 'interpret', return_value=intent()) as interpreter, \
             contextlib.redirect_stdout(io.StringIO()), channel.test_rig.active():
            session.toggle()
            session.worker.join(timeout=2)
        interpreter.assert_called_once_with('base to the left 45')
        result, error = session.results.get_nowait()
        self.assertIsNone(error)
        self.assertEqual(result['completed_actions'], 1)
        self.assertAlmostEqual(result['results'][0]['target_coordinates']['_1'],math.radians(-35))

    def test_direction_save_is_local_bound_to_twin_and_saves_opposite(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(vc, 'mapping_path', return_value=Path(directory)/'directions.json'):
            result = vc.save_direction('shoulder_pan', 'left', 1, 'chip is face')
            self.assertEqual(result['signs'], {'left': 1, 'right': -1})
            self.assertEqual(vc.resolve_actions(intent(), __import__('json').loads(vc.mapping_path().read_text()), 'test-twin')[0]['delta_degrees'], 45)


if __name__ == '__main__':
    unittest.main()
