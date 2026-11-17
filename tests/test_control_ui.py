"""UI command boundary and interruption tests; no network, audio or hardware."""
import io
import os
from pathlib import Path
import signal
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'build'))
import control_ui as ui


class ControlTests(unittest.TestCase):
    def test_command_allowlist_and_finite_values(self):
        self.assertEqual(ui.command_args('jog wrist_roll -5'), ['jog', 'wrist_roll', '-5.0'])
        for text in ('jog wrist_roll nan', 'move gripper inf', 'jog fake 2', 'read extra', 'pose start', 'terminal'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                ui.command_args(text)

    def test_no_shell_expansion(self):
        self.assertIsNone(ui.command_args('echo $(touch /tmp/unwanted)'))
        with self.assertRaises(ValueError):
            ui.command_args('read && start')

    def test_launch_is_argument_vector_and_separate_process_group(self):
        console = ui.Console()
        with patch.object(ui.subprocess, 'Popen') as popen:
            console.launch(['python', 'worker', 'quoted text; $(no)'])
        self.assertEqual(popen.call_args.args[0][-1], 'quoted text; $(no)')
        if os.name == 'nt':
            self.assertEqual(popen.call_args.kwargs['creationflags'],
                             ui.subprocess.CREATE_NEW_PROCESS_GROUP)
            self.assertFalse(popen.call_args.kwargs['start_new_session'])
        else:
            self.assertTrue(popen.call_args.kwargs['start_new_session'])
        self.assertNotIn('shell', popen.call_args.kwargs)

    def test_demo_never_launches_process_or_microphone(self):
        console = ui.Console(demo=True)
        with patch.object(console, 'launch') as launch:
            console.submit('start', preview=False)
            console.submit(capture=True)
            console.stop()
        launch.assert_not_called()
        self.assertFalse(console.busy)

    def test_busy_request_is_rejected_without_queue(self):
        console = ui.Console()
        console.busy = True
        with patch.object(console, 'launch') as launch, self.assertRaises(ValueError):
            console.submit('start', preview=False)
        launch.assert_not_called()

    def test_stop_kills_pending_worker_and_descendants_before_driver_stop(self):
        console = ui.Console(demo=True)
        process = Mock(pid=1234)
        console.process = process
        if os.name == 'nt':
            with patch.object(ui.subprocess, 'run') as run:
                console.stop()
            run.assert_called_once_with(['taskkill', '/F', '/T', '/PID', '1234'],
                                        capture_output=True)
        else:
            with patch.object(ui.os, 'killpg') as kill:
                console.stop()
            kill.assert_called_once_with(1234, signal.SIGKILL)
        process.wait.assert_called_once()
        self.assertIsNone(console.process)

    def test_failed_launch_releases_busy_state(self):
        console = ui.Console()
        with patch.object(console, 'launch', side_effect=OSError('failed')), self.assertRaises(OSError):
            console.submit('read')
        self.assertFalse(console.busy)

    def test_old_worker_cannot_clear_replacement_or_append_results(self):
        console = ui.Console()
        old = Mock(stdout=io.StringIO('old result\n'))
        new = Mock()
        console.process = new
        console.busy = True
        console.collect(old)
        self.assertIs(console.process, new)
        self.assertTrue(console.busy)
        self.assertFalse(any(e['text'] == 'old result' for e in console.events))

    def test_taught_sequences_reuse_terminal_parser(self):
        self.assertEqual(ui.taught_labels('right then left then middle'), ['right', 'left', 'middle'])
        self.assertIsNone(ui.taught_labels('wrist roll plus five degrees'))

    def test_taught_execution_reuses_existing_smooth_console(self):
        import base_console
        import robot
        channel = Mock()
        with patch.object(robot, 'configured_channel', return_value=channel), \
                patch.object(base_console, 'run_sequence', return_value=[]) as run, \
                patch('sys.stdout', new_callable=io.StringIO):
            ui.run_taught(['right', 'middle'], False)
        run.assert_called_once_with(channel, ['right', 'middle'])
        channel.client.disconnect.assert_called_once()

    def test_pose_blocks_before_execution(self):
        import grok_planner
        import voice_control
        for action in ({'type': 'pose', 'pose': 'start'},):
            with self.subTest(action=action), patch.object(grok_planner, 'interpret', return_value={'actions': [action]}), \
                    patch.object(voice_control, 'execute_intent') as execute, \
                    patch('turn_log.TurnLog') as log, patch('sys.stdout', new_callable=io.StringIO) as out:
                ui.interpret('command', False)
                execute.assert_not_called()
                self.assertIn('blocked', out.getvalue())


if __name__ == '__main__':
    unittest.main()
