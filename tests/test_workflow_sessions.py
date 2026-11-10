"""Persistent Cyberwave lifecycle tests with synthetic registers and transport."""
import contextlib
import io
from pathlib import Path
import queue
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import math

sys.path[:0] = [str(Path(__file__).resolve().parents[1]/'build'), str(Path(__file__).parent)]
from cyberwave_workflow_driver import Workflow
from workflow_protocol import PROTOCOL
from workflow_fakes import add_torque_bus
import test_voice_targets
from test_voice_targets import RIGHT, action


class PersistentSessionTests(unittest.TestCase):
    def setUp(self):
        names=('shoulder_pan','shoulder_lift','elbow_flex','wrist_flex','wrist_roll','gripper')
        self.positions={n:0. for n in names}
        self.follower=SimpleNamespace(connected=True,torque_enabled=True,
            motors={n:SimpleNamespace(id=i) for i,n in enumerate(names,1)},
            get_observation=lambda:{n+'.pos':v for n,v in self.positions.items()})
        self.bus=add_torque_bus(self.follower)
        self.pending=queue.Queue(maxsize=1000)
        self.mqtt=Mock(topic_prefix='',client_id='sdk',connected=True)
        self.event=threading.Event()
        self.driver=Workflow(self.mqtt,'test','a'*32,self.follower,self.event,self.pending,
            {n:'degrees' for n in names},{},lambda v,*_:math.radians(v),lambda v,*_:math.degrees(v),lambda *_:(True,''))
        self.folded={n:0. for n in names if n!='shoulder_pan'}
        self.counter=0

    def send(self,kind,**fields):
        self.counter+=1
        request=dict(protocol=PROTOCOL,kind=kind,operation_id=self.driver.operation_id,
            request_id=str(self.counter),session_id='sdk',timestamp=1000,
            supported_pose_radians=self.folded)
        request.update(fields)
        self.mqtt.publish.reset_mock()
        with patch('time.time',return_value=1000):self.driver.receive(request)
        return self.mqtt.publish.call_args.args[1] if self.mqtt.publish.called else None

    def completed_segment(self):
        reply=self.send('segment',command=dict(type='absolute',joint='_1',target_radians=math.radians(RIGHT)),
            limits_radians=[math.radians(-RIGHT),math.radians(RIGHT)],speed_degrees_s=45)
        self.assertEqual(reply['status'],'accepted')
        while not self.pending.empty():
            item=self.pending.get_nowait()
            self.positions['shoulder_pan']=item['shoulder_pan.pos']
            self.pending.task_done()

    def release(self):
        self.completed_segment()
        reply=self.send('release')
        self.assertEqual(reply['status'],'released')
        self.assertEqual(reply['mode'],'freedrive')
        self.assertFalse(self.follower.torque_enabled)

    def test_two_voice_commands_reuse_one_driver_start(self):
        started=Mock()
        results,sent=test_voice_targets.VoiceTargetsTests().simulate(
            [action('right')],following_actions=[action('left')],on_driver_start=started)
        started.assert_called_once()
        self.assertEqual([r['status'] for r in results],['completed','completed'],results)
        self.assertEqual([r['completed_actions'] for r in results],[1,1])
        self.assertEqual(len([p for _,p in sent if p.get('kind')=='release']),2)
        self.assertNotIn({'command':'freedrive'},[p for _,p in sent])

    def test_rearm_after_hand_guiding_replaces_stale_goal_before_torque(self):
        self.driver.start()
        self.release()
        self.assertIs(self.follower._workflow_session,self.driver)
        self.positions['shoulder_pan']=0
        self.bus.regs['Present_Position']={n:1800 for n in self.positions}
        self.bus.regs['Goal_Position']={n:3200 for n in self.positions}
        self.bus.events.clear()
        self.assertTrue(self.driver.reserve('b'*32))
        self.assertFalse(self.follower.torque_enabled)
        reply=self.send('confirm')
        self.assertEqual(reply['status'],'ready')
        self.assertTrue(self.follower.torque_enabled)
        self.assertEqual(self.bus.regs['Goal_Position'],self.bus.regs['Present_Position'])
        hold=next(i for i,e in enumerate(self.bus.events) if isinstance(e,tuple) and e[:2]==('write','Goal_Position'))
        self.assertLess(hold,self.bus.events.index('enable'))
        self.assertFalse(self.driver.released)
        self.assertIn('persistent_freedrive',reply['capabilities'])

    def test_delayed_confirmation_from_finished_operation_cannot_reenable(self):
        self.release()
        self.bus.events.clear()
        self.assertEqual(self.send('confirm')['status'],'rejected')
        self.assertNotIn('enable',self.bus.events)
        self.assertFalse(self.follower.torque_enabled)
        self.assertTrue(self.driver.reserve('b'*32))
        self.assertIsNone(self.send('confirm',operation_id='a'*32))
        self.assertNotIn('enable',self.bus.events)

    def test_busy_or_stopped_runtime_is_not_restarted_or_reassigned(self):
        self.assertFalse(self.driver.reserve('b'*32))
        self.assertEqual(self.driver.operation_id,'a'*32)
        self.release()
        self.pending.put({'shoulder_pan.pos':0})
        self.assertFalse(self.driver.reserve('b'*32))
        self.pending.get_nowait();self.pending.task_done()
        self.event.set()
        self.assertFalse(self.driver.reserve('b'*32))
        self.assertFalse(self.follower.torque_enabled)

    def test_changed_fold_or_calibration_prevents_rearming(self):
        self.release()
        self.assertTrue(self.driver.reserve('b'*32))
        self.bus.events.clear()
        self.positions['wrist_flex']=10
        self.assertEqual(self.send('confirm')['status'],'rejected')
        self.assertNotIn('enable',self.bus.events)
        self.positions['wrist_flex']=0
        self.bus.regs['Homing_Offset']['shoulder_pan']=999
        self.assertEqual(self.send('confirm')['status'],'rejected')
        self.assertNotIn('enable',self.bus.events)
        self.assertFalse(self.follower.torque_enabled)

    def test_release_requires_completed_endpoint_and_verified_torque_state(self):
        self.completed_segment()
        self.positions['shoulder_pan']=0
        self.assertEqual(self.send('release')['status'],'rejected')
        self.assertTrue(self.follower.torque_enabled)
        self.positions['shoulder_pan']=RIGHT
        self.bus.fail_read='Torque_Enable'
        reply=self.send('release')
        self.assertEqual(reply['status'],'rejected')
        self.assertFalse(self.driver.released)
        self.assertFalse(self.driver.reserve('b'*32))

    def test_old_segment_and_unconfirmed_new_segment_do_not_queue(self):
        self.release()
        self.assertTrue(self.driver.reserve('b'*32))
        fields=dict(command=dict(type='absolute',joint='_1',target_radians=0.),
            limits_radians=[math.radians(-RIGHT),math.radians(RIGHT)],speed_degrees_s=45)
        self.assertIsNone(self.send('segment',operation_id='a'*32,**fields))
        self.assertEqual(self.send('segment',**fields)['status'],'rejected')
        self.assertTrue(self.pending.empty())
        self.assertFalse(self.follower.torque_enabled)

    def test_failed_powered_readback_is_never_confirmed_ready(self):
        self.release()
        self.assertTrue(self.driver.reserve('b'*32))
        self.bus.fail_read='Torque_Enable'
        self.assertEqual(self.send('confirm')['status'],'rejected')
        self.assertTrue(self.driver.released)


if __name__=='__main__':unittest.main()
