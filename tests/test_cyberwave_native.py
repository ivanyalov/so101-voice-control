"""Native driver ownership, startup and measurement regressions; no hardware."""
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'build'))
import cyberwave_native as native


class NativeTests(unittest.TestCase):
    def test_only_explicit_native_device_paths_are_accepted(self):
        self.assertEqual(native.native_port('/dev/cu.usbmodemABC'),'/dev/cu.usbmodemABC')
        for port in ('/dev/ttyACM0','socket://localhost:8300','/dev/cu../../../tmp/test','/tmp/tty',None):
            with self.subTest(port=port),self.assertRaises(ValueError):native.native_port(port)

    def test_missing_partial_nan_or_old_feedback_is_never_fresh(self):
        feedback=native.Feedback()
        full={f'_{i}':float(i) for i in range(1,7)}
        with patch.object(native.time,'time',return_value=100):
            self.assertTrue(feedback.record(full))
            self.assertEqual(feedback.snapshot()['feedback'],'fresh')
            for invalid in ({},None,{'_1':1},{**full,'_6':float('nan')},{**full,'_6':True}):
                self.assertFalse(feedback.record(invalid))
                state=feedback.snapshot()
                self.assertEqual(state['feedback'],'unavailable')
                self.assertIsNone(state['positions_radians'])
            feedback.record(full)
        with patch.object(native.time,'time',return_value=102):
            self.assertEqual(feedback.snapshot()['feedback'],'unavailable')

    def guards(self):
        class Free:
            read_joint_positions=Mock()
        class Follower:
            get_observation=Mock()
        driver=SimpleNamespace(handle_command=Mock(),logger=Mock(),Cyberwave=Mock(),_edge_health=None,
                               SO101_JOINT_STREAM_ID='joints',SO101_JOINT_STREAM_RATE_HZ=30,_current_thread=None,
                               _stop_current_operation=Mock())
        modules={'utils.cw_freedrive':SimpleNamespace(FreedriveSession=Free),
                 'so101.follower':SimpleNamespace(SO101Follower=Follower),
                 'utils.utils':SimpleNamespace(normalized_to_radians=lambda v,*_:v)}
        original=driver.handle_command
        with patch.dict(sys.modules,modules):
            native.install_guards(driver,native.Feedback())
        return driver,original,Free,Follower

    def test_joint_health_counts_actual_measurements_instead_of_thread_or_port_presence(self):
        driver,_,Free,_=self.guards()
        driver._edge_health=Mock(edge_id='twin')
        driver._so101_joint_stream_config_provider()
        driver._edge_health.update_frame_count.assert_not_called()
        Free.read_joint_positions.__wrapped__.return_value={f'_{i}':float(i) for i in range(1,7)}
        Free().read_joint_positions()
        driver._so101_joint_stream_config_provider()
        driver._so101_joint_stream_config_provider()
        driver._edge_health.update_frame_count.assert_called_once()

    def test_no_automatic_controller_calibration_or_legacy_power_request(self):
        driver,original,_,_=self.guards()
        with patch.object(native.time,'time',return_value=100):
            for cmd,payload in [('controller-changed',{}),('calibrate',{}),('write_position',{}),
                                ('remoteoperate',{}),('remoteoperate',{'workflow_request':'a'*32}),
                                ('freedrive',{}),('freedrive',{'timestamp':80}),
                                ('remoteoperate',{'workflow_request':'a'*32,'timestamp':80}),
                                ('remoteoperate',{'workflow_request':'a'*32,'timestamp':True})]:
                driver.handle_command(Mock(),'twin',cmd,payload)
        original.assert_not_called()
        driver._ensure_setup('twin')
        driver._ensure_so101_urdf()
        driver._refresh_so101_ports_in_setup()
        driver._check_startup_calibration(Mock(),'twin')
        driver._evaluate_and_drive(Mock(),'twin')
        original.assert_not_called()

    def test_mqtt_loss_uses_local_stop_hold_without_rearming_or_releasing(self):
        driver,original,Free,Follower=self.guards()
        on_trip=Mock()
        self.assertTrue(driver._handle_mqtt_connection_lost('client','twin',on_trip))
        driver._stop_current_operation.assert_called_once_with('client','twin')
        original.assert_not_called()
        on_trip.assert_not_called()
        Free.read_joint_positions.__wrapped__.assert_not_called()
        Follower.get_observation.__wrapped__.assert_not_called()

    def test_fresh_explicit_workflow_and_freedrive_and_manual_stop_keep_official_handler(self):
        driver,original,_,_=self.guards()
        with patch.object(native.time,'time',return_value=100):
            for cmd,payload in [('remoteoperate',{'workflow_request':'a'*32,'timestamp':100}),
                                ('freedrive',{'timestamp':100}),('stop',{})]:
                driver.handle_command('client','twin',cmd,payload)
                original.assert_called_with('client','twin',cmd,payload)
        self.assertEqual(original.call_count,3)

    def test_native_worker_probe_replies_without_opening_a_motor_port(self):
        driver,original,Free,Follower=self.guards()
        client=SimpleNamespace(mqtt=Mock(topic_prefix=''))
        with patch.object(native.time,'time',return_value=100):
            driver.handle_command(client,'twin','native-status',dict(timestamp=100,request_id='a'*32))
        original.assert_not_called()
        Free.read_joint_positions.__wrapped__.assert_not_called()
        Follower.get_observation.__wrapped__.assert_not_called()
        topic,payload=client.mqtt.publish.call_args.args
        self.assertTrue(topic.endswith('/native/status/'+'a'*32))
        self.assertEqual(payload['attempts'],0)
        self.assertEqual(payload['feedback'],'unavailable')

    def test_persistent_empty_or_partial_freedrive_reads_force_reconnect(self):
        _,_,Free,_=self.guards()
        free=Free()
        original=Free.read_joint_positions.__wrapped__
        original.return_value={}
        self.assertEqual([free.read_joint_positions() for _ in range(5)],[{},{},{},{},None])
        original.return_value={'_1':1}
        self.assertIsNone(free.read_joint_positions())
        original.return_value={f'_{i}':float(i) for i in range(1,7)}
        self.assertEqual(len(free.read_joint_positions()),6)
        original.return_value={}
        self.assertEqual(free.read_joint_positions(),{})

    def test_invalid_powered_observation_keeps_the_official_empty_read_contract(self):
        _,_,_,Follower=self.guards()
        follower=Follower()
        Follower.get_observation.__wrapped__.return_value={}
        self.assertEqual(follower.get_observation(),{})

    @unittest.skipUnless(sys.platform == 'darwin', 'launchd is macOS-only')
    def test_activation_refuses_connected_arm_before_changing_any_services(self):
        modules={'serial.tools':SimpleNamespace(list_ports=SimpleNamespace(comports=lambda:[]))}
        real_exists=Path.exists
        with tempfile.TemporaryDirectory() as directory,patch.object(native,'STATE',Path(directory)),\
             patch.dict(sys.modules,modules),patch.object(native,'verify',return_value={'port':'/dev/cu.test'}),\
             patch.object(native.Path,'exists',autospec=True,side_effect=lambda p:str(p)=='/dev/cu.test' or real_exists(p)),\
             patch.object(native,'call') as call:
            with self.assertRaisesRegex(RuntimeError,'Disconnect the arm'):native.activate()
            call.assert_not_called()

    @unittest.skipUnless(sys.platform == 'darwin', 'launchd is macOS-only')
    def test_switch_stops_old_owners_before_starting_native_and_records_rollback(self):
        commands=[]
        def call(args,**kwargs):
            commands.append(args)
            stdout='{} true' if args[:2]==['docker','inspect'] else ''
            return SimpleNamespace(returncode=0,stdout=stdout)
        with tempfile.TemporaryDirectory() as directory,patch.object(native,'STATE',Path(directory)),\
             patch.object(native,'verify',return_value={'port':'/dev/cu.test','twin':'abc-def'}),\
             patch.object(native,'require_disconnected'),patch.object(native,'call',side_effect=call):
            native.activate()
            state=json.loads((Path(directory)/'activation.json').read_text())
        stop=next(i for i,c in enumerate(commands) if c[:2]==['docker','stop'])
        bootstrap=next(i for i,c in enumerate(commands) if c[:2]==['launchctl','bootstrap'])
        self.assertLess(stop,bootstrap)
        for label in native.LEGACY:
            self.assertLess(commands.index(['launchctl','disable',native.domain()+'/'+label]),stop)
            self.assertLess(commands.index(['launchctl','bootout',native.domain()+'/'+label]),stop)
        self.assertTrue(state['container_running'])
        self.assertEqual(state['stage'],'native_started')

    @unittest.skipUnless(sys.platform == 'darwin', 'launchd is macOS-only')
    def test_rollback_restores_original_restart_policy_and_service_enabled_state(self):
        state=dict(stage='native_started',container='old-driver',container_running=True,
            restart_policy={'Name':'unless-stopped','MaximumRetryCount':0},
            legacy={native.LEGACY[0]:dict(loaded=True,disabled=False),
                    native.LEGACY[1]:dict(loaded=False,disabled=True)})
        with tempfile.TemporaryDirectory() as directory,patch.object(native,'STATE',Path(directory)),\
             patch.object(native,'verify',return_value={}),patch.object(native,'require_disconnected'),\
             patch.object(native,'call',return_value=SimpleNamespace(returncode=1)) as call:
            native.write_json(Path(directory)/'activation.json',state)
            native.rollback()
        commands=[c.args[0] for c in call.call_args_list]
        self.assertEqual(commands[0],['launchctl','bootout',native.domain()+'/'+native.LABEL])
        self.assertIn(['docker','update','--restart=unless-stopped','old-driver'],commands)
        self.assertIn(['launchctl','enable',native.domain()+'/'+native.LEGACY[0]],commands)
        self.assertIn(['launchctl','disable',native.domain()+'/'+native.LEGACY[1]],commands)
        self.assertEqual(commands[-1],['docker','start','old-driver'])

    def test_status_does_not_reuse_a_dead_process_last_fresh_measurement(self):
        with tempfile.TemporaryDirectory() as directory,patch.object(native,'STATE',Path(directory)),\
             patch.object(native,'verify',return_value={'port':'/dev/cu.not-present'}):
            native.write_json(Path(directory)/'status.json',dict(timestamp=100,last_measured=100,
                feedback='fresh',positions_radians={'_1':1}))
            with patch.object(native.time,'time',return_value=105):
                state=native.status()
            self.assertFalse(state['native_process_running'])
            self.assertEqual(state['feedback'],'unavailable')
            self.assertIsNone(state['positions_radians'])

    def test_altered_bundle_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory,patch.object(native,'STATE',Path(directory)),\
             patch.object(native,'SOURCE',Path(directory)/'source'):
            p=Path(directory)/'source/main.py';p.parent.mkdir();p.write_text('pass\n')
            native.write_json(Path(directory)/'manifest.json',dict(port='/dev/cu.test',
                sha256={'source/main.py':native.digest(p)}))
            native.verify()
            p.write_text('raise SystemExit\n')
            with self.assertRaisesRegex(RuntimeError,'changed'):native.verify()


if __name__=='__main__':unittest.main()
