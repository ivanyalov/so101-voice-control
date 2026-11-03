"""Safety boundary and sequencing tests; fake transport, no motor writes."""
import copy
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import Mock, patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'build'))
import grok_planner as gp
import robot_control as rc
import voice_toggle


def action(direction='left', amount=5, arm='both', joint='shoulder_pan', kind='relative'):
    return dict(type=kind, joint=joint, direction=direction, amount_degrees=amount, pose=None, arm=arm)


def intent(actions=None):
    return dict(status='planned', reply='Rotate both bases left, then right.',
                actions=actions if actions is not None else [action(), action('right', 30)])


def fixture():
    config = rc.load_config()
    snapshot = {}
    for role, start in (('follower', 2300), ('leader', 2500)):
        snapshot[role] = {}
        config['arms'][role]['joints'] = {'shoulder_pan': {'verified': True, 'direction_sign': {'left': -1, 'right': 1}}}
        for motor_id, joint in enumerate(rc.JOINTS, 1):
            cal = dict(id=motor_id, drive_mode=0, homing_offset=0, range_min=500, range_max=3500)
            state = dict(present_position=start, homing_offset=0, range_min=500, range_max=3500,
                         operating_mode=0, voltage_tenths=120, temperature_c=25,
                         max_torque=1000, torque_limit=1000, position_p=16, status=0)
            snapshot[role][joint] = dict(calibration=cal, state=state, issues=[])
    return config, snapshot


class RobotPlanningTests(unittest.TestCase):
    def test_grok_schema_explicitly_supports_pair_without_changing_legacy_schema(self):
        body = None
        def handle(req):
            nonlocal body
            body = json.loads(req.content)
            return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(intent())}}]})
        client_type = httpx.Client
        with patch.dict(os.environ, {'XAI_API_KEY':'fixture-key','XAI_MODEL':'fixture-model'}), \
             patch.object(gp.httpx, 'Client', side_effect=lambda **kw: client_type(transport=httpx.MockTransport(handle), **kw)):
            actual = gp.interpret('Move leader and follower 5 degrees left then 30 right', paired=True)
        self.assertEqual([a['arm'] for a in actual['actions']], ['both', 'both'])
        self.assertIn('arm', body['response_format']['json_schema']['schema']['properties']['actions']['items']['required'])
        self.assertNotIn('arm', gp.SCHEMA['properties']['actions']['items']['required'])

    def test_sequential_relative_targets_use_each_arms_own_start(self):
        config, snapshot = fixture()
        result, steps = rc.resolve(intent(), snapshot, config)
        self.assertEqual(result['status'], 'resolved_measured')
        self.assertEqual([[(m['arm'],m['before_ticks'],m['target_ticks']) for m in g] for g in steps], [
            [('follower',2300,2243),('leader',2500,2443)],
            [('follower',2243,2584),('leader',2443,2784)]])
        self.assertEqual(result['executed_actions'], 0)

    def test_per_arm_signs_are_independent(self):
        config, snapshot = fixture()
        config['arms']['leader']['joints']['shoulder_pan']['direction_sign']['left'] = 1
        _, steps = rc.resolve(intent([action()]), snapshot, config)
        self.assertEqual([m['target_ticks']-m['before_ticks'] for m in steps[0]], [-57,57])

    def test_unverified_sign_is_not_inferred_from_mock_or_calibration(self):
        config, snapshot = fixture()
        config['arms']['leader']['joints'] = {}
        result, steps = rc.resolve(intent(), snapshot, config)
        self.assertEqual(result['status'], 'needs_configuration')
        self.assertEqual(steps, [])
        self.assertIsNone(result['motion_plan'])
        self.assertEqual(result['code_preview'], [])

    def test_leader_disabled_blocks_both_before_any_write(self):
        config, snapshot = fixture()
        state = snapshot['leader']['shoulder_pan']['state']
        state.update(max_torque=0, torque_limit=0, position_p=0)
        snapshot['leader']['shoulder_pan']['issues'] = rc.health_issues('leader','shoulder_pan',snapshot['leader']['shoulder_pan']['calibration'],state)
        with patch.object(rc, 'RobotBus') as factory:
            factory.return_value.snapshot.return_value = snapshot
            factory.return_value.write_count = 0
            result = rc.Controller(execute=True,config=config).process_intent(intent())
        self.assertEqual(result['status'], 'blocked_hardware')
        factory.return_value.execute.assert_not_called()
        factory.return_value.write.assert_not_called()
        self.assertEqual(result['executed_actions'],0)

    def test_out_of_range_second_step_rejects_whole_plan(self):
        config, snapshot = fixture()
        snapshot['leader']['shoulder_pan']['calibration']['range_max'] = 2600
        result, steps = rc.resolve(intent(), snapshot, config, execute=True)
        self.assertEqual(result['status'], 'needs_configuration')
        self.assertIsNone(result['motion_plan'])
        self.assertEqual(steps, [])

    def test_endpoints_poses_gripper_and_large_jumps_block(self):
        config,snapshot=fixture()
        for a in (action(amount=None,kind='endpoint'),action(amount=31), action(joint='gripper',direction='open')):
            with self.subTest(a=a):
                result,steps=rc.resolve(intent([a]),snapshot,config)
                self.assertEqual(result['status'],'needs_configuration')
                self.assertEqual(steps,[])

    def test_cancel_during_grok_cannot_later_execute(self):
        controller=rc.Controller(execute=True,config=fixture()[0])
        def interpret(*args,**kw):
            controller.stop()
            return intent()
        with patch.object(gp,'interpret',side_effect=interpret),patch.object(rc,'RobotBus') as bus:
            result=controller.process('move both arms left')
        self.assertEqual(result['status'],'cancelled')
        bus.assert_not_called()

    def test_verified_gripper_supports_explicit_40_motor_degrees(self):
        config, snapshot = fixture()
        config['arms']['follower']['joints']['gripper'] = {
            'verified': True, 'direction_sign': {'open': 1, 'close': -1}}
        result, steps = rc.resolve(intent([action(joint='gripper', direction='open',
                                               amount=40, arm='follower')]), snapshot, config)
        self.assertEqual(result['status'], 'resolved_measured')
        self.assertEqual(steps[0][0]['target_ticks'] - steps[0][0]['before_ticks'], 455)
        result, steps = rc.resolve(intent([action(joint='gripper', direction='open',
                                               amount=61, arm='follower')]), snapshot, config)
        self.assertEqual(steps, [])

    def test_edge_start_only_allows_target_inside_margin(self):
        config, snapshot = fixture()
        snapshot['follower']['shoulder_pan']['state']['present_position'] = 3499
        result, steps = rc.resolve(intent([action(arm='follower')]), snapshot, config)
        self.assertEqual(result['status'], 'resolved_measured')
        result, steps = rc.resolve(intent([action(direction='right', arm='follower')]), snapshot, config)
        self.assertEqual(steps, [])

    def test_hardware_preview_never_calls_executor_even_with_valid_targets(self):
        config,snapshot=fixture()
        with patch.object(rc,'RobotBus') as factory:
            factory.return_value.snapshot.return_value=snapshot
            factory.return_value.write_count=0
            result=rc.Controller(config=config).process_intent(intent())
        self.assertEqual(result['status'],'resolved_measured')
        factory.return_value.execute.assert_not_called()
        factory.return_value.write.assert_not_called()
        factory.return_value.close.assert_called_once()

    def test_bad_arm_or_missing_arm_rejected(self):
        for a in (dict(action(),arm='other'), {k:v for k,v in action().items() if k!='arm'}):
            with self.subTest(a=a),self.assertRaises(ValueError):
                gp.parse_intent(json.dumps(intent([a])),paired=True)


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        config,snapshot=fixture()
        self.result,self.steps=rc.resolve(intent([action(amount=5)]),snapshot,config,execute=True)
        self.bus=rc.RobotBus(config)
        self.bus.write=Mock()
        self.bus.profile=Mock()
        self.stop=threading.Event()

    def test_stale_start_causes_no_write(self):
        self.bus.position=Mock(return_value=999)
        with self.assertRaisesRegex(RuntimeError,'moved after planning'):
            self.bus.execute(self.steps,self.stop,self.result)
        self.bus.write.assert_not_called()

    def test_stop_before_first_write(self):
        self.stop.set()
        starts={m['arm']:m['before_ticks'] for m in self.steps[0]}
        self.bus.position=Mock(side_effect=lambda r,i:starts[r])
        with self.assertRaises(InterruptedError):
            self.bus.execute(self.steps,self.stop,self.result)
        self.bus.write.assert_not_called()

    def test_success_counts_dispatched_and_measured_actions(self):
        positions={m['arm']:m['before_ticks'] for m in self.steps[0]}
        def write(role,motor_id,name,value):
            if name=='goal':positions[role]=value
        self.bus.write.side_effect=write
        self.bus.profile.side_effect=lambda role,motor_id,target:write(role,motor_id,'goal',target)
        self.bus.position=Mock(side_effect=lambda r,i:positions[r])
        self.bus.read=Mock(side_effect=lambda r,i,addr,size=2:120 if addr==62 else 25 if addr==63 else 0)
        self.bus.execute(self.steps,self.stop,self.result)
        self.assertEqual(self.result['status'],'executed_holding')
        self.assertEqual(self.result['executed_actions'],2)
        self.assertEqual(self.result['completed_actions'],2)
        self.assertEqual(len(self.result['measured_results']),2)
        self.assertTrue(all(c.args[1]==1 for c in self.bus.write.call_args_list))

    def test_lost_torque_ack_still_marks_motor_for_hold(self):
        starts={m['arm']:m['before_ticks'] for m in self.steps[0]}
        self.bus.position=Mock(side_effect=lambda r,i:starts[r])
        def write(role,motor_id,name,value):
            if name=='torque':raise RuntimeError('lost ack')
        self.bus.write.side_effect=write
        with self.assertRaisesRegex(RuntimeError,'lost ack'):
            self.bus.execute(self.steps,self.stop,self.result)
        self.assertIn(('follower',1),self.bus.touched)
        self.bus.write.reset_mock(side_effect=True)
        self.assertEqual(self.bus.hold(),[])
        self.bus.write.assert_called_once_with('follower',1,'goal',2300)

    def test_timeout_after_targets_dispatch_preserves_partial_execution_and_holds(self):
        config,snapshot=fixture()
        bus=rc.RobotBus(config)
        starts={m['arm']:m['before_ticks'] for m in self.steps[0]}
        bus.position=Mock(side_effect=lambda r,i:starts[r])
        bus.read=Mock(side_effect=lambda r,i,addr,size=2:120 if addr==62 else 25 if addr==63 else 0)
        bus.write=Mock()
        bus.profile=Mock()
        with patch.object(rc.time,'monotonic',side_effect=[0,100]),self.assertRaisesRegex(RuntimeError,'timeout'):
            bus.execute(self.steps,self.stop,self.result)
        self.assertEqual(self.result['executed_actions'],2)
        self.assertEqual(self.result['completed_actions'],0)
        self.assertEqual(len(bus.touched),2)
        self.assertEqual(bus.hold(),[])

    def test_cancel_is_not_cleared_after_transcription(self):
        controller=rc.Controller(config=fixture()[0])
        controller.stop()
        with patch.object(gp,'interpret') as llm:
            result=controller.process('move left')
        llm.assert_not_called()
        self.assertEqual(result['status'],'cancelled')

    def test_mock_execute_and_basic_hardware_are_rejected(self):
        for kwargs in ({'execute':True},{'robot':'pair','mock_state':True},{'robot':'pair','planner':'basic'}):
            with self.subTest(kwargs=kwargs),self.assertRaises(ValueError):
                voice_toggle.run(**kwargs)


if __name__=='__main__':unittest.main()
