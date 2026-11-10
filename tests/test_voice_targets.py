"""Grok intent -> exact targets -> real Cyberwave serializer, synthetic driver only."""
import contextlib
import io
import json
import math
import os
import queue
from types import SimpleNamespace
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'build'))
import base_console
import grok_planner
from cyberwave_workflow_driver import Workflow
from workflow_protocol import PROTOCOL
import robot
from workflow_fakes import add_torque_bus
import voice_control as vc

RIGHT = 66.5934065934066
MIDDLE = -4.615384615384612


def action(direction, fraction=None):
    return dict(type='endpoint' if fraction is None else 'fraction', joint='shoulder_pan',
                direction=direction, amount_degrees=None, fraction=fraction, pose=None, arm='follower')


def intent(actions):
    return dict(status='planned', reply='Move the base to the requested targets.', actions=actions)


def profile():
    return {'twin': 'test-twin', 'joints': {'shoulder_pan': {'verified': True,
        'targets_degrees': {'right': RIGHT, 'left': -RIGHT, 'middle': MIDDLE}}}}


class VoiceTargetsTests(unittest.TestCase):
    def test_exact_endpoints_middle_and_fraction_preserve_order(self):
        commands = vc.resolve_actions(intent([action('right'), action('middle'), action('left'),
                                              action('middle'), action('right', .5)]), profile(), 'test-twin')
        self.assertEqual([c['target_degrees'] for c in commands], [RIGHT, MIDDLE, -RIGHT, MIDDLE, RIGHT / 2])
        self.assertEqual([c['target_radians'] for c in commands],
                         [math.radians(x) for x in [RIGHT, MIDDLE, -RIGHT, MIDDLE, RIGHT / 2]])

    def test_fraction_and_middle_cannot_be_misencoded(self):
        invalid = [action('positive'), action('middle', .5), action('right', 2),
                   action('right', True), action('left', float('nan')),
                   {**action('middle'), 'type': 'relative', 'amount_degrees': 5},
                   {**action('right', .5), 'amount_degrees': 30}]
        for a in invalid:
            with self.subTest(a=a), self.assertRaises(ValueError):
                grok_planner.parse_intent(json.dumps(intent([a])), paired=True)

    def test_wrong_twin_or_missing_later_target_blocks_before_connection(self):
        for p in [dict(profile(), twin='wrong'), {'twin': 'test-twin', 'joints': {}}]:
            factory = Mock()
            with patch.dict(os.environ, CYBERWAVE_TWIN_UUID='test-twin'):
                result = vc.execute_intent(intent([action('right'), action('middle')]),
                                           profile=p, channel_factory=factory)
            self.assertEqual(result['status'], 'blocked')
            factory.assert_not_called()

    def simulate(self, actions, *, cancel_after_first=False, change_session=False,
                 missing_feedback=False, folded=True, fail_second=False,
                 no_ack=False, negative_ack=False, unrelated_ack=False,
                 cancel_startup=False, disconnect_startup=False, driver_start=0,
                 client_start=None, fail_stop=False, fail_cleanup=False,
                 lose_feedback=False, delayed_ack=False, reject_segment=False,
                 lose_subscription=False, following_actions=None, on_driver_start=None):
        now = [0.0]
        current = [math.radians(driver_start)]
        session = ['sdk-client']  # A real SDK session is unchanged across modes.
        sent, subscribers, pending = [], {}, []
        cancelled = threading.Event()
        client = Mock()
        client.mqtt.connected = True
        client.mqtt.topic_prefix = ''
        channel = robot.Channel(client, 'test-twin')
        client.mqtt.is_subscribed.side_effect = lambda _: not lose_subscription
        driver = [None]
        follower = SimpleNamespace(connected=True, torque_enabled=True,
            motors={n: SimpleNamespace(id=i) for i,n in enumerate(robot.JOINTS,1)},
            get_observation=lambda: {n+'.pos': current[0] if n=='shoulder_pan' else 0 for n in robot.JOINTS})
        add_torque_bus(follower)
        driver_mqtt = Mock(topic_prefix='', client_id='sdk-client', connected=True)
        def reply(topic, payload, **kwargs):
            if negative_ack:
                payload = dict(payload, status='rejected', detail='Startup rejected')
            if unrelated_ack:
                payload = dict(payload, operation_id='unrelated')
            if not no_ack:
                subscribers[topic](payload)
            return True
        driver_mqtt.publish.side_effect = reply
        def feedback():
            if missing_feedback or (lose_feedback and channel.dispatched):
                return
            value = current[0]
            if client_start is not None and not channel.dispatched:
                value = math.radians(client_start)
            channel.receive({'source_type':'edge_follower', 'timestamp':1000+now[0],
                'session_id':session[0], 'positions':{j:value if j=='_1' else 0 for j in robot.JOINTS.values()}})
        def sleep(seconds):
            now[0] += seconds
            if no_ack and .05 <= now[0] < .1:
                channel.latest=None
                return
            while pending and pending[0][0] <= now[0]:
                _, current[0] = pending.pop(0)
            feedback()
        def publish(topic, payload, **options):
            sent.append((topic, payload))
            if payload.get('command') == 'remoteoperate':
                if driver[0] is not None:
                    driver[0].reserve(payload['workflow_request'])
                else:
                    driver[0] = Workflow(driver_mqtt, 'test-twin', payload['workflow_request'],
                        follower, threading.Event(), queue.Queue(maxsize=1000),
                        {n:'identity' for n in robot.JOINTS}, {},
                        lambda v,*_:v, lambda v,*_:v, lambda *_:(not reject_segment,'Position limit'))
                    if on_driver_start:
                        on_driver_start()
                now[0] += .001
                feedback()
                if cancel_startup: cancelled.set()
                if disconnect_startup: client.mqtt.connected = False
            elif payload.get('kind') in {'confirm','segment','release'}:
                if payload['kind'] == 'release' and fail_cleanup:
                    return False
                if fail_second and channel.dispatched == 1 and payload['kind']=='segment':
                    return False
                if not (delayed_ack and payload['kind']=='confirm' and now[0]<.5):
                    driver[0].receive(payload)
                if payload['kind']=='segment':
                    for item in list(driver[0].queue.queue):
                        pending.append((now[0]+item['_delta_meta']['local_offset'], item['shoulder_pan.pos']))
                    while not driver[0].queue.empty():
                        driver[0].queue.get_nowait()
                        driver[0].queue.task_done()
                    if cancel_after_first: cancelled.set()
                    if change_session:
                        session[0]='unexpected'
            elif payload.get('command') == 'stop' and fail_stop:
                return False
            elif payload.get('command') == 'freedrive' and fail_cleanup:
                return False
            return True
        def subscribe(topic, handler, **kwargs):
            subscribers[topic] = handler
            feedback()
        client.mqtt.publish.side_effect = publish
        client.mqtt.subscribe.side_effect = subscribe
        folded_pose = {name:0 if folded else 10 for name in robot.JOINTS}
        with patch.dict(os.environ, CYBERWAVE_TWIN_UUID='test-twin'), \
             patch.object(base_console.time, 'time', side_effect=lambda:1000+now[0]), \
             patch.object(base_console.time, 'monotonic', side_effect=lambda:now[0]), \
             patch.object(base_console.time, 'sleep', side_effect=sleep), \
             patch.object(base_console.poses, 'load_pose', return_value=folded_pose), \
             contextlib.redirect_stdout(io.StringIO()):
            result=vc.execute_intent(intent(actions),profile=profile(),channel_factory=lambda:channel,cancelled=cancelled)
            if following_actions is not None:
                first = result
                channel.dispatched = 0
                now[0] += .01
                feedback()
                second=vc.execute_intent(intent(following_actions),profile=profile(),channel_factory=lambda:channel,cancelled=cancelled)
                result=[first, second]
        return result,sent

    def test_exact_targets_use_versioned_driver_generated_trajectories(self):
        result,sent=self.simulate([action('right'),action('middle'),action('left'),action('middle')])
        self.assertEqual(result['status'],'completed',result)
        self.assertEqual(result['executed_actions'],4)
        self.assertEqual(result['completed_actions'],4)
        self.assertEqual(result['acknowledged_actions'],4)
        payloads=[(t,p) for t,p in sent if p.get('kind')=='segment']
        self.assertEqual(len(payloads),4)
        for (topic,payload),target in zip(payloads,[RIGHT,MIDDLE,-RIGHT,MIDDLE]):
            self.assertEqual(topic,'cyberwave/twin/test-twin/workflow/v1/request')
            self.assertEqual(payload['protocol'],PROTOCOL)
            self.assertEqual(payload['command'],{'type':'absolute','joint':'_1','target_radians':math.radians(target)})
            self.assertNotIn('positions',payload)
            self.assertNotIn('chunks',payload)
        self.assertEqual(sent[-1][1]['kind'],'release')
        self.assertNotIn({'command':'freedrive'}, [p for _,p in sent])

    def test_halfway_right_is_absolute_after_left(self):
        result, _ = self.simulate([action('left'), action('right', .5)])
        self.assertEqual(result['status'], 'completed', result)
        self.assertEqual([r['target_degrees'] for r in result['results']], [-RIGHT, RIGHT / 2])

    def test_lost_feedback_or_unsupported_fold_never_enables_power(self):
        for options in ({'missing_feedback': True}, {'folded': False}):
            result, sent = self.simulate([action('right')], **options)
            self.assertEqual(result['executed_actions'], 0)
            self.assertEqual(sent, [])

    def test_cancel_or_session_change_prevents_later_segment(self):
        for options in ({'cancel_after_first': True}, {'change_session': True}):
            result, sent = self.simulate([action('right'), action('left')], **options)
            self.assertEqual(result['status'], 'execution_failed')
            self.assertEqual(result['executed_actions'], 1)
            self.assertEqual(len([p for _, p in sent if p.get('kind') == 'segment']), 1)
            self.assertTrue(any(p.get('command') == 'stop' for _,p in sent))
            self.assertNotIn({'command': 'freedrive'}, [p for _, p in sent])

    def test_failed_later_segment_retains_completed_count(self):
        result, _ = self.simulate([action('right'), action('left')], fail_second=True)
        self.assertEqual(result['status'], 'execution_failed')
        self.assertEqual(result['executed_actions'], 1)
        self.assertEqual(result['completed_actions'], 1)


if __name__ == '__main__':
    unittest.main()
