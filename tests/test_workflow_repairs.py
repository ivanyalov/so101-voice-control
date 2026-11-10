"""Offline protocol and driver-boundary regressions; no services or motor I/O."""
import ast
import contextlib
import io
import json
import logging
import math
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'build'), str(ROOT/'tests')]
import base_console
import control_ui
import cyberwave_image
from cyberwave_workflow_driver import Workflow
import cyberwave_trajectory
import robot
import voice_control as vc
import voice_toggle
from workflow_protocol import PROTOCOL, trajectory
from test_voice_targets import action, intent, profile, RIGHT, MIDDLE
import test_voice_targets


def segments(sent):
    return [p for _,p in sent if p.get('kind')=='segment']


def copied_function(source, name, env, method=False):
    tree=ast.parse(source)
    nodes=ast.walk(tree) if method else tree.body
    node=next(n for n in nodes if isinstance(n,ast.FunctionDef) and n.name==name)
    exec(compile('from __future__ import annotations\n'+ast.unparse(node),'<driver source>','exec'),env)
    return env[name]


class DriverBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.obs={n+'.pos':0 for n in robot.JOINTS}
        self.follower=SimpleNamespace(connected=True,torque_enabled=True,
            motors={n:SimpleNamespace(id=i) for i,n in enumerate(robot.JOINTS,1)},
            get_observation=lambda:dict(self.obs))
        self.mqtt=Mock(topic_prefix='',client_id='sdk-session',connected=True)
        self.queue=queue.Queue(maxsize=1000)
        self.stop=threading.Event()
        self.driver=Workflow(self.mqtt,'test-twin','operation',self.follower,self.stop,self.queue,
            {n:'degrees' for n in robot.JOINTS},{},lambda v,*_:math.radians(v),
            lambda v,*_:math.degrees(v),lambda *_:(True,''))

    def request(self, **updates):
        req=dict(protocol=PROTOCOL,kind='segment',operation_id='operation',request_id='segment',
            session_id='sdk-session',timestamp=1000,command=dict(type='absolute',joint='_1',target_radians=math.radians(RIGHT)),
            limits_radians=[math.radians(-RIGHT),math.radians(RIGHT)],speed_degrees_s=45,
            supported_pose_radians={n:0 for n in robot.JOINTS if n!='shoulder_pan'})
        req.update(updates)
        with patch('time.time',return_value=1000):
            self.driver.receive(req)
        return self.mqtt.publish.call_args.args[1] if self.mqtt.publish.called else None

    def test_absolute_target_is_independent_of_client_and_driver_start(self):
        for start in (-2,0,2):
            with self.subTest(driver_start=start):
                self.obs['shoulder_pan.pos']=start
                reply=self.request(request_id=str(start))
                self.assertEqual(reply['status'],'accepted')
                self.assertAlmostEqual(list(self.queue.queue)[-1]['shoulder_pan.pos'],RIGHT)
                values=[math.radians(a['shoulder_pan.pos']) for a in self.queue.queue]
                self.assertEqual(reply['target_radians'],math.radians(RIGHT))
                self.assertLessEqual(max(abs(b-a)*30 for a,b in zip(values,values[1:])),math.radians(45))
                self.driver.close()

    def test_second_absolute_and_relative_commands_retain_semantics(self):
        self.obs['shoulder_pan.pos']=-RIGHT
        r=self.request(command=dict(type='absolute',joint='_1',target_radians=math.radians(RIGHT/2)))
        self.assertEqual(r['target_radians'],math.radians(RIGHT/2))
        self.driver.close()
        self.obs['shoulder_pan.pos']=2
        r=self.request(request_id='relative',command=dict(type='relative',joint='_1',delta_radians=math.radians(5)))
        self.assertAlmostEqual(math.degrees(r['target_radians']),7)

    def test_invalid_or_unsupported_requests_never_queue_actions(self):
        for updates in (
            {'command':dict(type='absolute',joint='_1',target_radians=float('nan'))},
            {'command':dict(type='absolute',joint='_1',target_radians=math.radians(90))},
            {'command':dict(type='absolute',joint='_1',target_radians=True)},
            {'command':dict(type='absolute',joint='_2',target_radians=0)},
            {'command':dict(type='other',joint='_1',target_radians=0)},
            {'command':None}, {'command':[]},
            {'session_id':'other'},{'timestamp':900}, {'speed_degrees_s':float('inf')},
            {'limits_radians':[0,0]}, {'supported_pose_radians':{}},
            {'operation_id':'other'},{'protocol':'unsupported.v2'}):
            with self.subTest(updates=updates):
                self.request(**updates)
                self.assertTrue(self.queue.empty())

    def test_complete_fresh_measurement_and_folded_pose_required(self):
        for key,value in (('wrist_roll.pos',None),('elbow_flex.pos',float('nan')),('wrist_flex.pos',10)):
            previous=self.obs[key]
            self.obs[key]=value
            self.assertEqual(self.request()['status'],'rejected')
            self.assertTrue(self.queue.empty())
            self.obs[key]=previous
        self.stop.set()
        self.assertEqual(self.request(kind='confirm')['status'],'rejected')

    def test_all_limits_are_checked_before_atomic_enqueue(self):
        self.driver.validate=lambda _,v:(abs(v)<20,'test limit')
        self.assertEqual(self.request()['status'],'rejected')
        self.assertEqual(self.queue.unfinished_tasks,0)
        self.assertTrue(self.queue.empty())
        self.driver.validate=lambda *_:(True,'')
        self.queue.maxsize=1
        self.assertEqual(self.request()['status'],'rejected')
        self.assertTrue(self.queue.empty())

    def test_duplicate_or_busy_segment_is_rejected_without_partial_enqueue(self):
        self.assertEqual(self.request()['status'],'accepted')
        count=self.queue.qsize()
        self.assertEqual(self.request()['status'],'rejected')
        self.assertEqual(self.request(request_id='next')['status'],'rejected')
        self.assertEqual(self.queue.qsize(),count)
        self.driver.close()
        self.assertEqual(self.queue.unfinished_tasks,0)

    def test_unsubscribe_failure_still_drains_stopped_queue(self):
        self.request()
        self.mqtt.unsubscribe.side_effect=RuntimeError('unsubscribe')
        with self.assertRaises(RuntimeError):self.driver.close()
        self.assertEqual(self.queue.unfinished_tasks,0)

    def test_smoothstep_duration_and_deceleration(self):
        values,duration=trajectory(math.radians(-RIGHT),dict(type='absolute',joint='_1',target_radians=math.radians(RIGHT)),
                                   [math.radians(-RIGHT),math.radians(RIGHT)])
        speeds=[(b-a)*30 for a,b in zip(values,values[1:])]
        self.assertEqual(duration,(len(values)-1)/30)
        self.assertLess(speeds[0],max(speeds)/10)
        self.assertLess(speeds[-1],max(speeds)/10)
        self.assertLessEqual(max(speeds),math.radians(45))


class ClientWorkflowTests(unittest.TestCase):
    def simulate(self,*args,**kwargs):
        return test_voice_targets.VoiceTargetsTests().simulate(*args,**kwargs)

    def test_old_session_gap_unrelated_or_negative_ack_never_dispatches(self):
        for options in ({'no_ack':True},{'unrelated_ack':True},{'negative_ack':True},
                        {'cancel_startup':True},{'disconnect_startup':True},{'lose_subscription':True}):
            with self.subTest(options=options):
                result,sent=self.simulate([action('right')],**options)
                self.assertEqual(result['executed_actions'],0,result)
                self.assertEqual(segments(sent),[])
                self.assertNotIn({'command':'freedrive'},[p for _,p in sent])
                self.assertEqual(result['dispatch_state'],'blocked_before_dispatch')
                self.assertIn('No motion target published',result['note'])

    def test_delayed_correlated_ack_with_unchanged_sdk_session_can_run(self):
        result,sent=self.simulate([action('right')],delayed_ack=True)
        self.assertEqual(result['status'],'completed',result)
        self.assertEqual(len(segments(sent)),1)

    def test_distinct_driver_and_client_measurements_preserve_absolute_target(self):
        for start in (-2,2):
            result,_=self.simulate([action('right')],driver_start=start,client_start=0)
            self.assertEqual(result['status'],'completed',result)
            self.assertAlmostEqual(result['results'][0]['measured_degrees'],RIGHT)

    def test_cancellation_and_lost_feedback_preserve_stop_without_freedrive(self):
        for options in ({'cancel_after_first':True},{'change_session':True},{'lose_feedback':True},
                        {'cancel_after_first':True,'fail_stop':True}):
            with self.subTest(options=options):
                result,sent=self.simulate([action('right'),action('left')],**options)
                self.assertEqual(result['status'],'execution_failed',result)
                self.assertEqual(len(segments(sent)),1)
                controls=[p['command'] for _,p in sent if isinstance(p.get('command'),str)]
                self.assertEqual(controls,['remoteoperate','stop'])
                self.assertFalse(any(p.get('kind')=='release' for _,p in sent))
                if options.get('fail_stop'):self.assertTrue(result['cleanup_errors'])

    def test_cleanup_failure_does_not_erase_measured_completions(self):
        result,sent=self.simulate([action('right')],fail_cleanup=True)
        self.assertEqual(result['status'],'execution_failed')
        self.assertEqual(result['completed_actions'],1)
        self.assertEqual(result['executed_actions'],1)
        self.assertTrue(any(p.get('command')=='stop' for _,p in sent))

    def test_explicit_driver_rejection_is_distinct_from_uncertain_delivery(self):
        result,_=self.simulate([action('right')],reject_segment=True)
        self.assertEqual(result['published_actions'],1)
        self.assertEqual(result['acknowledged_actions'],0)
        self.assertEqual(result['completed_actions'],0)
        self.assertEqual(result['dispatch_state'],'rejected_by_driver')

    def test_out_of_range_sequence_blocks_before_mode_change(self):
        invalid=action('right',.5)
        p=profile()
        p['joints']['shoulder_pan']['targets_degrees']['right']=float('nan')
        factory=Mock()
        with patch.dict(os.environ,CYBERWAVE_TWIN_UUID='test-twin'):
            result=vc.execute_intent(intent([invalid]),profile=p,channel_factory=factory)
        self.assertEqual(result['status'],'blocked')
        factory.assert_not_called()


class PreparationAndTimingTests(unittest.TestCase):
    def test_preview_and_execution_share_prepared_contract(self):
        request=intent([action('left'),action('right',.5)])
        with patch.dict(os.environ,CYBERWAVE_TWIN_UUID='test-twin'), \
             patch.object(vc,'load_profile',return_value=profile()), \
             patch.object(vc.grok_planner,'interpret',return_value=request), \
             patch.object(vc,'TurnLog'),patch.object(vc.robot,'configured_channel') as factory, \
             contextlib.redirect_stdout(io.StringIO()):
            preview=vc.process_text('left then halfway right',preview=True)
            prepared=vc.prepare_intent(request)
        factory.assert_not_called()
        for key in ('resolved_commands','prepared_commands','execution_format','transport_requirements'):
            self.assertEqual(preview[key],prepared[key])
        self.assertFalse(preview['motion_sent'])
        self.assertEqual([c['target_radians'] for c in preview['prepared_commands']],
                         [math.radians(-RIGHT),math.radians(RIGHT/2)])

    def test_browser_natural_language_uses_same_processor(self):
        with patch.object(vc,'process_text',return_value={'status':'preview'}) as process,patch('sys.stdout'):
            control_ui.interpret('all the way right',True)
        process.assert_called_once_with('all the way right',preview=True,timeout=30)

    def test_sign_save_preserves_taught_targets_and_other_joint_metadata(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(vc,'mapping_path',return_value=Path(tmp)/'directions.json'), \
             patch.dict(os.environ,CYBERWAVE_TWIN_UUID='test-twin'):
            p=profile()
            p['joints']['shoulder_pan']['source']='hand-guided'
            vc.mapping_path().write_text(json.dumps(p))
            vc.save_direction('shoulder_pan','right',1,'new viewpoint')
            saved=json.loads(vc.mapping_path().read_text())
        self.assertEqual(saved['joints']['shoulder_pan']['targets_degrees'],p['joints']['shoulder_pan']['targets_degrees'])
        self.assertEqual(saved['joints']['shoulder_pan']['source'],'hand-guided')

    def test_transcription_processor_and_interpretation_timings_are_distinct(self):
        processor=Mock(return_value={'status':'execution_failed','interpretation_elapsed_seconds':.2,
            'preparation_elapsed_seconds':.01,'connection_preflight_elapsed_seconds':15.,
            'execution_confirmation_elapsed_seconds':0.,'executed_actions':0})
        session=voice_toggle.Session(Mock(),processor=processor)
        with patch.object(voice_toggle.readiness,'transcribe',return_value={'text':'right'}), \
             patch.object(voice_toggle.time,'perf_counter',side_effect=[0,1,16.21]),patch('sys.stdout'):
            session._transcribe(Path('fake.wav'))
        result,error=session.results.get_nowait()
        self.assertIsNone(error)
        self.assertEqual(result['interpretation_elapsed_seconds'],.2)
        self.assertEqual(result['connection_preflight_elapsed_seconds'],15)
        self.assertEqual(result['transcription_elapsed_seconds'],1)
        self.assertEqual(result['processing_elapsed_seconds'],16.21)
        self.assertNotIn('planning_elapsed_seconds',result)

    def test_real_processor_keeps_interpretation_separate_from_execution_wait(self):
        now=[0.]
        def interpret(*a,**kw):now[0]+=.2;return intent([action('right')])
        def execute(*a,**kw):
            now[0]+=15
            return {**kw['prepared'],'status':'execution_failed','connection_preflight_elapsed_seconds':15}
        with patch.object(vc.time,'perf_counter',side_effect=lambda:now[0]), \
             patch.object(vc,'load_profile',return_value=profile()),patch.dict(os.environ,CYBERWAVE_TWIN_UUID='test-twin'), \
             patch.object(vc.grok_planner,'interpret',side_effect=interpret),patch.object(vc,'execute_intent',side_effect=execute), \
             patch.object(vc,'TurnLog'),patch('sys.stdout'):
            result=vc.process_text('right')
        self.assertEqual(result['interpretation_elapsed_seconds'],.2)
        self.assertEqual(result['preparation_elapsed_seconds'],0)
        self.assertEqual(result['processing_elapsed_seconds'],15.2)


class PatchedSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.context=Path(cls.tmp.name)
        files={
            'main.py':'.cache/cyberwave-edge-so101/main.py',
            'so101/follower.py':'.cache/cyberwave-safe-image/follower.py.original',
            'scripts/cw_remoteoperate.py':'.cache/cyberwave-safe-image/cw_remoteoperate.py.original',
            'utils/cw_remoteoperate_helpers.py':'.cache/robot/cw_remoteoperate_helpers.before-smooth.py'}
        if not all((ROOT/p).exists() for p in files.values()):
            raise unittest.SkipTest('Reviewed read-only driver sources unavailable; use pinned image source export.')
        for target,source in files.items():
            dest=cls.context/target
            dest.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(ROOT/source,dest)
        for patch_name in ('cyberwave-preserve-pose.patch','cyberwave-local-trajectory.patch'):
            subprocess.run(['patch','--batch','--fuzz=0','-p1','-i',str(ROOT/'build'/patch_name)],
                           cwd=cls.context,check=True,capture_output=True)
        cyberwave_image.verify_workflow_inputs(cls.context)
        cls.legacy=(cls.context/'utils/cw_remoteoperate_helpers.py').read_text()
        subprocess.run(['patch','--batch','--fuzz=0','-p1','-i',str(ROOT/'build/cyberwave-workflow-v1.patch')],
                       cwd=cls.context,check=True,capture_output=True)

    def test_exact_patches_apply_and_all_patched_sources_compile(self):
        for p in self.context.rglob('*.py'):compile(p.read_text(),str(p),'exec')
        with self.assertRaisesRegex(RuntimeError,'Unreviewed driver source'):
            cyberwave_image.verify_workflow_inputs(self.context)

    def test_real_command_entry_reuses_idle_runtime_and_never_restarts_a_busy_one(self):
        source=(self.context/'main.py').read_text()
        for available in (True, False):
            workflow=Mock()
            workflow.reserve.return_value=available
            stop, idle=Mock(), Mock()
            env={'_current_follower':SimpleNamespace(_workflow_session=workflow),
                 '_stop_current_operation':stop, '_stop_idle_camera_streaming':idle, 'logger':Mock()}
            start=copied_function(source,'start_remoteoperate',env)
            start(Mock(),'test',workflow_request='b'*32)
            workflow.reserve.assert_called_once_with('b'*32)
            stop.assert_not_called()
            idle.assert_not_called()

    def test_late_scoped_stop_cannot_stop_a_newer_workflow_owner(self):
        source=(self.context/'main.py').read_text()
        for requested in ('a'*32, 'b'*32, None):
            stop=Mock()
            env={'HANDEYE_ALERT_CLOSED_COMMAND':'unused', 'SUPPORTED_COMMANDS':{'stop'},
                 '_current_follower':SimpleNamespace(_workflow_session=SimpleNamespace(operation_id='b'*32)),
                 '_stop_current_operation':stop, '_start_idle_camera_streaming':Mock()}
            handle=copied_function(source,'handle_command',env)
            payload={} if requested is None else {'workflow_request':requested}
            handle(Mock(),'test','stop',payload)
            if requested=='a'*32:stop.assert_not_called()
            else:stop.assert_called_once()

    def test_workflow_cold_start_uses_readiness_without_fixed_settle_sleep(self):
        source=(self.context/'main.py').read_text()
        outer=next(n for n in ast.parse(source).body if isinstance(n,ast.FunctionDef) and n.name=='start_remoteoperate')
        for operation in ('a'*32, None):
            sleep, remote, follower=Mock(), Mock(), Mock()
            modules={'scripts.cw_remoteoperate':SimpleNamespace(remoteoperate=remote),
                     'so101.follower':SimpleNamespace(SO101Follower=Mock(return_value=follower)),
                     'utils.config':SimpleNamespace(FollowerConfig=Mock())}
            env=dict(client=Mock(), twin_uuid='test', workflow_request=operation,
                logger=Mock(), time=SimpleNamespace(sleep=sleep),
                create_robot_setup_alert=Mock(), so101=SimpleNamespace(cameras=[]),
                materialize_camera_entries_for_edge_operation=Mock(return_value=[]),
                follower_port='synthetic', max_relative_target=None, follower_id='test',
                operation_stop_event=threading.Event(), DeviceConnectionError=RuntimeError)
            run=copied_function(ast.unparse(outer),'_run',env,method=True)
            with patch.dict(sys.modules,modules):run()
            follower.connect.assert_called_once()
            remote.assert_called_once()
            self.assertEqual(remote.call_args.kwargs['workflow_request'],operation)
            if operation is None:sleep.assert_called_once_with(10.)
            else:sleep.assert_not_called()

    def test_managed_freedrive_loop_rejects_legacy_motion_payloads(self):
        source=(self.context/'scripts/cw_remoteoperate.py').read_text()
        for operation in ('a'*32, None):
            legacy=Mock()
            update=copied_function(source,'legacy_joint_update',{
                'workflow_request':operation,'joint_state_callback':legacy},method=True)
            payload={'source_type':'tele','positions':{'_1':.5}}
            update('cyberwave/joint/test/update',payload)
            if operation is None:legacy.assert_called_once()
            else:legacy.assert_not_called()

    def test_older_real_callback_ignores_dedicated_protocol_topic(self):
        env={'process_multi_joint_update':Mock(),'process_delta_update':Mock(),
             'process_chunked_delta_update':Mock(),'process_single_joint_update':Mock()}
        fn=copied_function(self.legacy,'create_joint_state_callback',env)
        callback=fn({},queue.Queue(),{},{},Mock())
        callback('cyberwave/twin/test/workflow/v1/request',{'source_type':'tele','protocol':PROTOCOL,'command':{'type':'absolute','joint':'_1','target_radians':1}})
        for value in env.values():
            if isinstance(value,Mock):value.assert_not_called()

    def test_failed_physical_reads_never_return_cached_measurement(self):
        env={'SerialException':RuntimeError,'logger':Mock()}
        fn=copied_function((self.context/'so101/follower.py').read_text(),'get_observation',env,method=True)
        follower=Mock(connected=True,_current_positions={'shoulder_pan.pos':22})
        follower.bus.sync_read.return_value={}
        self.assertEqual(fn(follower),{})
        follower.bus.sync_read.side_effect=RuntimeError('corrupt packet')
        self.assertEqual(fn(follower),{})

    def test_actual_driver_normalization_preserves_radian_endpoint(self):
        utils=ROOT/'.cache/cyberwave-edge-so101/utils/utils.py'
        if not utils.exists():self.skipTest('Read-only driver conversion source unavailable')
        modes=SimpleNamespace(RANGE_M100_100='signed',RANGE_0_100='unsigned')
        env={'math':math,'MotorNormMode':modes}
        to_rad=copied_function(utils.read_text(),'normalized_to_radians',env)
        from_rad=copied_function(utils.read_text(),'radians_to_normalized',env)
        calib=SimpleNamespace(range_min=1024,range_max=3072)
        for mode in ('signed','unsigned','degrees'):
            for target in (RIGHT,MIDDLE,-RIGHT,RIGHT/2):
                radians=math.radians(target)
                self.assertAlmostEqual(to_rad(from_rad(radians,mode,calib),mode,calib),radians,places=12)

    def test_collision_aborts_segment_and_preserves_queue_accounting(self):
        source=(self.context/'utils/cw_remoteoperate_helpers.py').read_text()
        env={'HighLoadDebouncer':Mock,'queue':queue,'logger':logging.getLogger(__name__),
             'HIGH_LOAD_THRESHOLD':900,'DELTA_META_KEY':'_delta_meta','wait_for_chunk':lambda *_:True}
        fn=copied_function(source,'motor_writer_worker',env)
        pending=queue.Queue()
        for _ in range(2):pending.put({'shoulder_pan.pos':0,'_delta_meta':{'workflow_v1':True}})
        follower=Mock(_pin_model=object())
        follower.validate_action_kinematics.return_value=(False,'test collision')
        stopped=threading.Event()
        fn(pending,follower,stopped)
        self.assertTrue(stopped.is_set())
        follower.send_action.assert_not_called()
        self.assertEqual(pending.unfinished_tasks,1)
        pending.get_nowait();pending.task_done()
        self.assertEqual(pending.unfinished_tasks,0)

    def test_writer_stall_drains_segment_without_writes(self):
        source=(self.context/'utils/cw_remoteoperate_helpers.py').read_text()
        def stalled(*_):raise RuntimeError('Local trajectory writer stalled')
        env={'HighLoadDebouncer':Mock,'queue':queue,'logger':Mock(),
             'HIGH_LOAD_THRESHOLD':900,'DELTA_META_KEY':'_delta_meta','wait_for_chunk':stalled}
        fn=copied_function(source,'motor_writer_worker',env)
        pending=queue.Queue()
        for _ in range(3):pending.put({'shoulder_pan.pos':0,'_delta_meta':{'workflow_v1':True}})
        follower=Mock()
        stopped=threading.Event()
        fn(pending,follower,stopped)
        self.assertTrue(stopped.is_set())
        follower.send_action.assert_not_called()
        self.assertEqual(pending.unfinished_tasks,0)
        self.assertTrue(pending.empty())

    def test_driver_adapter_through_real_writer_keeps_local_timing_and_exact_endpoint(self):
        source=(self.context/'utils/cw_remoteoperate_helpers.py').read_text()
        clock=[0.]
        stopped=threading.Event()
        def wait(delay):clock[0]+=delay;return stopped.is_set()
        event=Mock(is_set=stopped.is_set,set=stopped.set,wait=wait)
        env={'HighLoadDebouncer':Mock,'queue':queue,'logger':Mock(),
             'HIGH_LOAD_THRESHOLD':900,'DELTA_META_KEY':'_delta_meta',
             'wait_for_chunk':cyberwave_trajectory.wait_for_chunk}
        writer=copied_function(source,'motor_writer_worker',env)
        pending=queue.Queue(maxsize=1000)
        follower=Mock(connected=True,torque_enabled=True,_pin_model=object())
        follower.motors={n:SimpleNamespace(id=i) for i,n in enumerate(robot.JOINTS,1)}
        follower.get_observation.return_value={n+'.pos':2 if n=='shoulder_pan' else 0 for n in robot.JOINTS}
        follower.validate_action_kinematics.return_value=(True,'')
        mqtt=Mock(topic_prefix='',client_id='sdk',connected=True)
        driver=Workflow(mqtt,'test','op',follower,event,pending,{n:'degrees' for n in robot.JOINTS},{},
            lambda v,*_:math.radians(v),lambda v,*_:math.degrees(v),lambda *_:(True,''))
        request=dict(protocol=PROTOCOL,kind='segment',operation_id='op',request_id='segment',session_id='sdk',
            command=dict(type='absolute',joint='_1',target_radians=math.radians(RIGHT)),
            limits_radians=[math.radians(-RIGHT),math.radians(RIGHT)],speed_degrees_s=45,
            supported_pose_radians={n:0 for n in robot.JOINTS if n!='shoulder_pan'},timestamp=1000)
        with patch('time.time',return_value=1000):driver.receive(request)
        reply=mqtt.publish.call_args.args[1]
        self.assertEqual(reply['status'],'accepted')
        count=pending.qsize()
        writes=[]
        def send(action):
            writes.append((clock[0],action['shoulder_pan.pos']))
            clock[0]+=.005  # Real serial writes consume time.
            if len(writes)==count:stopped.set()
        follower.send_action.side_effect=send
        with patch.object(cyberwave_trajectory.time,'monotonic',side_effect=lambda:clock[0]):
            writer(pending,follower,event,enable_load_safety=False)
        self.assertEqual(len(writes),count)
        self.assertEqual(follower.validate_action_kinematics.call_count,count)
        self.assertAlmostEqual(writes[0][1],2)
        self.assertAlmostEqual(writes[-1][1],RIGHT)
        self.assertAlmostEqual(writes[-1][0],reply['duration_seconds'])
        for i,(stamp,_) in enumerate(writes):self.assertAlmostEqual(stamp,i/30)
        self.assertEqual(pending.unfinished_tasks,0)

    def run_coordinated_writer(self, *, lose_geometry=False, collide_in_writer=False, stall=False):
        from coordinated_fakes import Rig
        from coordinated_driver import require_collision_model
        from test_coordinated_motion import six_actions, intent as coordinated_intent
        from workflow_protocol import COORDINATED
        rig=Rig()
        source=(self.context/'utils/cw_remoteoperate_helpers.py').read_text()
        env={'HighLoadDebouncer':Mock,'queue':queue,'logger':Mock(),
             'HIGH_LOAD_THRESHOLD':900,'DELTA_META_KEY':'_delta_meta',
             'wait_for_chunk':cyberwave_trajectory.wait_for_chunk,
             'require_collision_model':require_collision_model}
        writer=copied_function(source,'motor_writer_worker',env)
        # Use the saved driver's actual conversion functions with a distinct
        # CalibrationEntry representation, just as resolve_calibration_for_edge does.
        utils=(ROOT/'.cache/cyberwave-edge-so101/utils/utils.py').read_text()
        modes=SimpleNamespace(RANGE_M100_100=rig.modes['shoulder_pan'],RANGE_0_100=rig.modes['gripper'])
        converters={'math':math,'MotorNormMode':modes}
        to_rad=copied_function(utils,'normalized_to_radians',converters)
        from_rad=copied_function(utils,'radians_to_normalized',converters)
        with rig.active():
            rig.channel.connect()
            operation='a'*32
            rig.subscribers['cyberwave/twin/test-twin/workflow/v1/response']=lambda _:None
            rig.publish('command',dict(command='remoteoperate',workflow_request=operation))
            rig.driver.to_rad,rig.driver.from_rad=to_rad,from_rad
            class OtherCalibration:
                pass
            calibration={}
            for n,c in rig.follower.calibration.items():
                calibration[n]=OtherCalibration()
                for k,v in vars(c).items():setattr(calibration[n],k,v)
            rig.driver.calibration=calibration
            def request(kind,**fields):
                rig.driver.receive(dict(protocol=PROTOCOL,kind=kind,operation_id=operation,
                    request_id=kind,session_id=rig.session,timestamp=1000+rig.now,**fields))
                self.assertNotEqual(rig.responses[-1]['status'],'rejected',rig.responses[-1])
            request('confirm',motion_profile=COORDINATED)
            prepared=vc.prepare_intent(coordinated_intent(six_actions()),profile={},twin='test-twin')
            request('segment',command=prepared['prepared_commands'][0])
            reply=rig.responses[-1]
            pending=rig.driver.queue
            count=pending.qsize()
            if lose_geometry:rig.follower._pin_collision_model=None
            if collide_in_writer:rig.follower.validate_action_kinematics.return_value=(False,'collision')
            stopped=threading.Event()
            def wait(delay):rig.now+=delay;return stopped.is_set()
            event=Mock(is_set=stopped.is_set,set=stopped.set,wait=wait)
            writes=[]
            rig.follower.config=SimpleNamespace(max_relative_target=None)
            actual_send=copied_function((self.context/'so101/follower.py').read_text(),
                'send_action',{},method=True)
            def send(action):
                writes.append((rig.now,dict(action)))
                sent=actual_send(rig.follower,action)
                self.assertEqual(set(sent),{n+'.pos' for n in robot.JOINTS})
                rig.now+=.005 if not stall else .2
                if len(writes)==count:stopped.set()
            rig.follower.send_action=send
            writer(pending,rig.follower,event,enable_load_safety=False)
            self.assertTrue(stopped.is_set())
            rig.driver.close()  # remoteoperate teardown drains a stopped writer's remainder.
        self.assertEqual(pending.unfinished_tasks,0)
        self.assertTrue(pending.empty())
        return rig,reply,writes,count

    def test_six_joint_adapter_uses_actual_normalization_and_30hz_patched_writer(self):
        rig,reply,writes,count=self.run_coordinated_writer()
        self.assertEqual(len(writes),count)
        for i,(stamp,action) in enumerate(writes):
            self.assertAlmostEqual(stamp-writes[0][0],i/30)
            self.assertEqual(set(action),{n+'.pos' for n in robot.JOINTS})
        for name,key in robot.JOINTS.items():
            norm=writes[-1][1][name+'.pos']
            actual=norm if name=='gripper' else rig.driver.to_rad(norm,rig.modes[name],rig.driver.calibration[name])
            self.assertAlmostEqual(actual,reply['target_coordinates'][key])
        self.assertAlmostEqual(writes[-1][0]-writes[0][0],reply['duration_seconds'])

    def test_missing_geometry_or_collision_aborts_actual_coordinated_writer(self):
        for options in ({'lose_geometry':True},{'collide_in_writer':True}):
            with self.subTest(options=options):
                _,_,writes,_=self.run_coordinated_writer(**options)
                self.assertEqual(writes,[])

    def test_actual_coordinated_writer_aborts_a_local_scheduling_stall(self):
        _,_,writes,_=self.run_coordinated_writer(stall=True)
        self.assertEqual(len(writes),1)


if __name__=='__main__':unittest.main()
