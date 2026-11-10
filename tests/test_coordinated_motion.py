"""Coordinated intent, driver trajectory, lifecycle and all-joint supervision."""
import copy
import json
import math
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from types import SimpleNamespace

sys.path[:0]=[str(Path(__file__).resolve().parents[1]/'build'),str(Path(__file__).parent)]
import grok_planner
import voice_control as vc
from workflow_protocol import (JOINTS, COORDINATED, coordinated_command,
    coordinated_trajectory, coordinated_target)
from coordinated_fakes import Rig
from test_voice_targets import profile, RIGHT, MIDDLE


def move(joint,amount,group=0):
    return dict(type='relative',joint=joint,direction='positive' if amount>0 else 'negative',
        amount_degrees=abs(amount),amount_percent=None,fraction=None,pose=None,arm='follower',group=group)


def opening(percent,group=0):
    return dict(type='opening',joint='gripper',direction=None,amount_degrees=None,
        amount_percent=percent,fraction=None,pose=None,arm='follower',group=group)


def intent(actions):
    return dict(status='planned',reply='Requested coordinated movement.',actions=actions)


def six_actions():
    return [move(n,a) for n,a in zip(list(JOINTS)[:5],[20,10,-15,5,25])]+[opening(60)]


class IntentTests(unittest.TestCase):
    def test_six_actions_form_one_segment_with_explicit_gripper_units(self):
        result=vc.prepare_intent(intent(six_actions()),profile={},twin='test-twin')
        self.assertEqual(result['execution_format'],COORDINATED)
        self.assertEqual(len(result['prepared_commands']),1)
        commands=result['prepared_commands'][0]['joints']
        self.assertEqual(set(commands),set(JOINTS.values()))
        self.assertEqual(commands['_6'],dict(type='absolute',value=60,unit='percent'))
        self.assertEqual(commands['_3']['value'],math.radians(-15))

    def test_voice_handler_uses_grouped_converter_executor_and_all_joint_feedback(self):
        import voice_toggle
        rig=Rig()
        session=voice_toggle.Session(None,processor=vc.process_text)
        with rig.active(), \
             patch.object(voice_toggle.readiness,'transcribe',return_value={'text':'All six joints together.'}), \
             patch.object(grok_planner,'interpret',return_value=intent(six_actions())) as interpret, \
             patch.object(vc,'load_profile',return_value={}), \
             patch.object(vc.robot,'configured_channel',return_value=rig.channel), \
             patch.object(vc,'TurnLog'):
            session._transcribe(Path('synthetic.wav'))
            result,error=session.results.get_nowait()
        self.assertIsNone(error)
        self.assertEqual(result['status'],'completed',result)
        self.assertEqual(result['completed_actions'],1)
        self.assertEqual(len(result['results'][0]['participating_joints']),6)
        interpret.assert_called_once_with('All six joints together.',follower_only=True)

    def test_simultaneous_then_sequential_preserves_groups_and_taught_targets(self):
        endpoint=dict(type='endpoint',joint='shoulder_pan',direction='right',amount_degrees=None,
            amount_percent=None,fraction=None,pose=None,arm='follower',group=0)
        result=vc.prepare_intent(intent([endpoint,move('elbow_flex',10),opening(30,1)]),profile=profile(),twin='test-twin')
        self.assertEqual(len(result['prepared_commands']),2)
        self.assertEqual(result['prepared_commands'][0]['joints']['_1']['value'],math.radians(RIGHT))
        self.assertEqual(set(result['prepared_commands'][0]['joints']),{'_1','_3'})

    def test_invalid_groups_duplicates_and_gripper_units_reject_before_connection(self):
        invalid=[[move('elbow_flex',5),move('elbow_flex',10)],
            [move('elbow_flex',5,1)], [move('elbow_flex',5,0),move('wrist_flex',5,2)],
            [move('elbow_flex',5,True)], [opening(101)], [opening(float('nan'))],
            [move('gripper',10)], [{**opening(50),'direction':'open'}],
            [move('wrist_flex',5),{k:v for k,v in move('elbow_flex',5).items() if k!='group'}]]
        for actions in invalid:
            with self.subTest(actions=actions),self.assertRaises(ValueError):
                vc.resolve_actions(intent(actions),{},'test-twin')

    def test_unverified_physical_direction_and_saved_pose_remain_blocked(self):
        for action in [{**move('shoulder_lift',10),'direction':'up'},
                       dict(type='pose',joint=None,direction=None,amount_degrees=None,pose='start',arm='follower')]:
            self.assertEqual(vc.prepare_intent(intent([action]),profile={},twin='test-twin')['status'],'blocked')


class DriverAndExecutorTests(unittest.TestCase):
    def test_six_joints_share_samples_and_finish_together_then_hold(self):
        rig=Rig()
        with rig.active():result=rig.execute(intent(six_actions()))
        self.assertEqual(result['status'],'completed',result)
        self.assertEqual(result['published_actions'],1)
        self.assertEqual(result['completed_actions'],1)
        self.assertEqual(result['final_mode'],'hold')
        self.assertTrue(rig.follower.torque_enabled)
        self.assertTrue(rig.driver.coordinated.finished)
        self.assertTrue(all(set(a)-{'_delta_meta'}=={n+'.pos' for n in JOINTS} for _,a in rig.writes))
        for name in JOINTS:
            start,end=rig.writes[0][1][name+'.pos'],rig.writes[-1][1][name+'.pos']
            self.assertNotEqual(start,end)
            self.assertNotEqual(start,rig.writes[1][1][name+'.pos'])
            self.assertNotEqual(end,rig.writes[-2][1][name+'.pos'])
        self.assertAlmostEqual(rig.norm['gripper'],60)

    def test_together_then_next_group_waits_for_all_joint_completion(self):
        rig=Rig()
        actions=[move('shoulder_lift',10),move('elbow_flex',-10),opening(70,1)]
        with rig.active():result=rig.execute(intent(actions))
        self.assertEqual(result['status'],'completed',result)
        self.assertEqual(result['completed_actions'],2)
        self.assertEqual([r['participating_joints'] for r in result['results']],[['_2','_3'],['_6']])
        self.assertGreater(result['results'][1]['timestamp'],result['results'][0]['timestamp'])

    def test_omitted_joints_hold_the_driver_measured_start(self):
        rig=Rig()
        rig.norm['wrist_flex']=7
        held=dict(rig.norm)
        with rig.active():result=rig.execute(intent([move('elbow_flex',15)]))
        self.assertEqual(result['status'],'completed',result)
        for _,sample in rig.writes:
            for name in set(JOINTS)-{'elbow_flex'}:
                self.assertAlmostEqual(sample[name+'.pos'],held[name])

    def test_return_to_folded_pose_releases_and_reuses_same_driver(self):
        rig=Rig()
        with rig.active():
            first=rig.execute(intent([move('elbow_flex',10)]))
            second=rig.execute(intent([move('elbow_flex',-10)]))
            third=rig.execute(intent([move('wrist_roll',10)]))
        self.assertEqual([r['status'] for r in (first,second,third)],['completed']*3,(first,second,third))
        self.assertEqual([r['final_mode'] for r in (first,second,third)],['hold','freedrive','hold'])
        self.assertEqual(rig.starts,1)
        self.assertIn('disable',rig.bus.events)
        self.assertIn('enable',rig.bus.events)

    def test_base_only_group_continues_from_a_nonfolded_held_pose(self):
        rig=Rig()
        with rig.active():
            first=rig.execute(intent([move('elbow_flex',10)]),profile())
            second=rig.execute(intent([move('shoulder_pan',15)]),profile())
        self.assertEqual(first['status'],'completed',first)
        self.assertEqual(second['status'],'completed',second)
        self.assertEqual(second['final_mode'],'hold')
        self.assertEqual(rig.starts,1)

    def test_uncommanded_joint_drift_prevents_success(self):
        rig=Rig(drift_joint='wrist_roll')
        with rig.active():result=rig.execute(intent([move('elbow_flex',10)]))
        self.assertEqual(result['status'],'execution_failed',result)
        self.assertEqual(result['completed_actions'],0)
        self.assertNotIn('disable',rig.bus.events)

    def test_unavailable_collision_geometry_rejects_readiness(self):
        for attribute in ('_pin_data','_pin_collision_model','_pin_collision_data','_pin_floor_geom_id'):
            rig=Rig()
            setattr(rig.follower,attribute,None)
            with rig.active():result=rig.execute(intent(six_actions()))
            self.assertEqual(result['published_actions'],0,result)
            self.assertEqual(rig.writes,[])

    def test_expired_operation_and_finished_operation_cannot_replay_or_rearm(self):
        rig=Rig()
        with rig.active():
            result=rig.execute(intent(six_actions()))
            self.assertEqual(result['status'],'completed',result)
            confirm=next(p for _,p in rig.sent if p.get('kind')=='confirm')
            segment=next(p for _,p in rig.sent if p.get('kind')=='segment')
            old=rig.driver.operation_id
            self.assertTrue(rig.driver.reserve('b'*32))
            count=len(rig.responses)
            rig.driver.receive({**segment,'timestamp':1000+rig.now})
            self.assertEqual(len(rig.responses),count)
            self.assertTrue(rig.driver.queue.empty())
            rig.driver.receive({**segment,'operation_id':'b'*32,'timestamp':1000+rig.now})
            self.assertEqual(rig.responses[-1]['status'],'rejected')
            rig.driver.receive({**confirm,'operation_id':'b'*32,'timestamp':1000+rig.now})
            self.assertEqual(rig.responses[-1]['status'],'ready')
            self.assertTrue(rig.driver.queue.empty())

    def test_no_supported_folded_pose_means_successful_hold(self):
        rig=Rig()
        with rig.active(),patch('poses.load_pose',side_effect=ValueError('No saved pose')):
            result=rig.execute(intent([opening(5)]))
        self.assertEqual(result['status'],'completed',result)
        self.assertEqual(result['final_mode'],'hold')
        self.assertNotIn('disable',rig.bus.events)

    def test_failed_torque_readback_prevents_ready_or_successful_finish(self):
        rig=Rig()
        rig.bus.fail_read='Torque_Enable'
        with rig.active():result=rig.execute(intent(six_actions()))
        self.assertEqual(result['published_actions'],0,result)
        rig=Rig()
        original=rig.publish
        def publish(topic,payload,**kw):
            if payload.get('kind')=='finish':rig.bus.fail_read='Torque_Enable'
            return original(topic,payload,**kw)
        rig.client.mqtt.publish.side_effect=publish
        with rig.active():result=rig.execute(intent(six_actions()))
        self.assertEqual(result['status'],'execution_failed',result)
        self.assertEqual(result['completed_actions'],1)
        self.assertFalse(rig.driver.coordinated.finished)
        self.assertTrue(rig.driver.stop_event.is_set())

    def test_duplicate_segment_and_midflight_reservation_are_rejected(self):
        rig=Rig()
        original=rig.publish
        def publish(topic,payload,**kw):
            result=original(topic,payload,**kw)
            if payload.get('kind')=='segment':
                queued=rig.driver.queue.qsize()
                self.assertFalse(rig.driver.reserve('b'*32))
                rig.driver.receive(payload)
                self.assertEqual(rig.responses[-1]['status'],'rejected')
                self.assertEqual(rig.driver.queue.qsize(),queued)
            return result
        rig.client.mqtt.publish.side_effect=publish
        with rig.active():result=rig.execute(intent(six_actions()))
        self.assertEqual(result['status'],'completed',result)

    def test_missing_motor_measurement_rejects_driver_before_enqueue(self):
        rig=Rig()
        rig.follower.get_observation=lambda:{'elbow_flex.pos':0}
        with rig.active():result=rig.execute(intent(six_actions()))
        self.assertEqual(result['published_actions'],0,result)
        self.assertEqual(rig.writes,[])

    def test_one_lagging_joint_prevents_next_segment_and_release(self):
        rig=Rig(lost_joint='elbow_flex')
        with rig.active():result=rig.execute(intent(six_actions()+[opening(10,1)]))
        self.assertEqual(result['status'],'execution_failed',result)
        self.assertEqual(result['published_actions'],1)
        self.assertEqual(result['completed_actions'],0)
        self.assertNotIn('disable',rig.bus.events)
        self.assertFalse(any(p.get('kind')=='finish' for _,p in rig.sent))

    def test_invalid_later_target_prevents_all_motion_dispatch(self):
        rig=Rig()
        with rig.active():result=rig.execute(intent([move('elbow_flex',10),move('wrist_roll',1000,1)]))
        self.assertEqual(result['published_actions'],0,result)
        self.assertEqual(rig.writes,[])

    def test_taught_base_limits_are_not_expanded_by_other_joints(self):
        rig=Rig()
        with rig.active():result=rig.execute(intent([move('shoulder_pan',RIGHT+1),move('elbow_flex',5)]),profile())
        self.assertEqual(result['published_actions'],0,result)
        self.assertEqual(rig.writes,[])

    def test_startup_failures_dispatch_no_targets(self):
        for options in ({'no_capability':True},{'no_feedback':True},{'missing_collision_model':True},
                        {'lose_subscription':True}):
            with self.subTest(options=options):
                rig=Rig(**options)
                with rig.active():result=rig.execute(intent(six_actions()))
                self.assertEqual(result['published_actions'],0,result)
                self.assertEqual(rig.writes,[])

    def test_collision_or_position_rejection_queues_no_partial_trajectory(self):
        for options in ({'collision':True},{'reject_position':True}):
            rig=Rig(**options)
            with rig.active():result=rig.execute(intent(six_actions()))
            self.assertTrue(result['driver_rejected'],result)
            self.assertEqual(result['completed_actions'],0)
            self.assertTrue(rig.driver.queue.empty())
            self.assertNotIn('disable',rig.bus.events)

    def test_cancel_or_session_change_prevents_later_groups_and_freedrive(self):
        for options in ({'cancel_first':True},{'change_session':True}):
            rig=Rig(**options)
            with rig.active():result=rig.execute(intent(six_actions()+[opening(10,1)]))
            self.assertEqual(result['status'],'execution_failed',result)
            self.assertEqual(result['published_actions'],1)
            self.assertNotIn('disable',rig.bus.events)

    def test_failed_second_publish_preserves_first_measured_count(self):
        rig=Rig(fail_second=True)
        with rig.active():result=rig.execute(intent(six_actions()+[opening(10,1)]))
        self.assertEqual(result['published_actions'],1,result)
        self.assertEqual(result['completed_actions'],1)
        self.assertNotIn('disable',rig.bus.events)

    def test_shared_timing_respects_every_joint_peak_speed_and_exact_endpoint(self):
        rig=Rig()
        with rig.active():result=rig.execute(intent(six_actions()))
        response=next(r for r in rig.responses if r['kind']=='segment')
        specs=rig.driver.coordinated.specs
        values,duration=coordinated_trajectory(response['start_coordinates'],
            next(p['command'] for _,p in rig.sent if p.get('kind')=='segment'),specs)
        self.assertEqual(values[-1],response['target_coordinates'])
        self.assertEqual(duration,response['duration_seconds'])
        for key,spec in specs.items():
            speeds=[abs(b[key]-a[key])*30 for a,b in zip(values,values[1:])]
            self.assertLessEqual(max(speeds),spec['peak_speed']+1e-10)
            self.assertLess(speeds[0],max(speeds))
            self.assertLess(speeds[-1],max(speeds))


if __name__=='__main__':unittest.main()
