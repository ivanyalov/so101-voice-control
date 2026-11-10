"""Terminal -> Cyberwave MQTT -> paired SO101. No browser or serial access."""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
JOINTS = dict(zip(('shoulder_pan', 'shoulder_lift', 'elbow_flex',
                   'wrist_flex', 'wrist_roll', 'gripper'),
                  ('_1', '_2', '_3', '_4', '_5', '_6')))
MAX_AGE = 1.5


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


class Channel:
    def __init__(self, client, twin):
        self.client, self.twin = client, twin
        self.topic = f'{client.mqtt.topic_prefix}cyberwave/joint/{twin}/update'
        self.lock = threading.Lock()
        self.latest = None
        self.dispatched = 0

    def receive(self, data):
        if not isinstance(data, dict) or data.get('source_type') != 'edge_follower':
            return
        stamp, positions = data.get('timestamp'), data.get('positions')
        if not finite(stamp) or not -0.25 <= time.time() - stamp <= MAX_AGE:
            return
        if not isinstance(positions, dict) or not all(finite(positions.get(j)) for j in JOINTS.values()):
            return
        if not isinstance(data.get('session_id'), str) or not data['session_id']:
            return
        with self.lock:
            if self.latest and stamp <= self.latest['timestamp']:
                return
            self.latest = {'timestamp': stamp, 'received': time.monotonic(),
                           'positions': {j: positions[j] for j in JOINTS.values()},
                           'session_id': data['session_id']}

    def snapshot(self):
        with self.lock:
            state = self.latest
            if state and time.monotonic() - state['received'] <= MAX_AGE and -0.25 <= time.time() - state['timestamp'] <= MAX_AGE:
                return {**state, 'positions': dict(state['positions'])}
        return None

    def wait_state(self, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = self.snapshot()
            if state:
                return state
            time.sleep(0.03)
        raise RuntimeError('No fresh six-joint physical feedback. Robot disconnected or driver inactive; no movement sent.')

    def connect(self):
        self.client.mqtt.connect()
        self.client.mqtt.subscribe(self.topic, self.receive)

    def probe(self, timeout):
        # Official ping request topic: connectivity only, never a motion command.
        nonce = uuid.uuid4().hex
        topic = f'{self.client.mqtt.topic_prefix}cyberwave/ping/{self.twin}/request'
        received = threading.Event()
        self.client.mqtt.subscribe(topic, lambda d: received.set() if d.get('nonce') == nonce else None, qos=1)
        try:
            deadline = time.monotonic() + timeout
            while not self.client.mqtt.is_subscribed(topic):
                if time.monotonic() >= deadline:
                    raise RuntimeError('Cyberwave diagnostic subscription not acknowledged.')
                time.sleep(0.03)
            if not self.client.mqtt.publish(topic, {'nonce': nonce, 'type': 'ping', 'timestamp': time.time()}, qos=1):
                raise RuntimeError('Cyberwave rejected diagnostic publish.')
            if not received.wait(max(0, deadline - time.monotonic())):
                raise RuntimeError('No diagnostic message returned through Cyberwave broker.')
            return True
        finally:
            self.client.mqtt.unsubscribe(topic)

    def move(self, joint, degrees, *, relative=False, timeout=10, tolerance=1.5, cancelled=None):
        if joint not in JOINTS or not finite(degrees):
            raise ValueError('Use a supported joint and finite degree value.')
        if cancelled is not None and cancelled.is_set():
            raise RuntimeError('Command cancelled; no movement sent.')
        before = self.wait_state(timeout)
        key = JOINTS[joint]
        target = math.radians(degrees) + (before['positions'][key] if relative else 0)
        # Explicit signed coordinates avoid inventing physical left/up conventions.
        sent_at = time.time()
        if cancelled is not None and cancelled.is_set():
            raise RuntimeError('Command cancelled; no movement sent.')
        if not self.client.mqtt.connected or self.snapshot() is None:
            raise RuntimeError('Connection or physical feedback lost; no movement sent.')
        accepted = self.client.mqtt.publish(self.topic, {
            'source_type': 'tele', 'type': 'joint_state', 'joint_name': key,
            'joint_state': {'position': target}, 'timestamp': sent_at,
        }, qos=0)
        if not accepted:
            raise RuntimeError('Motion publish rejected locally; execution unconfirmed.')
        self.dispatched += 1
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = self.snapshot()
            if state is None:
                raise RuntimeError('Feedback lost after dispatch; execution unconfirmed. No retry sent.')
            if state['session_id'] != before['session_id']:
                raise RuntimeError('Robot session changed after dispatch; execution unconfirmed.')
            if state['timestamp'] > sent_at and abs(state['positions'][key] - target) <= math.radians(tolerance):
                return {'status': 'measured_within_tolerance', 'joint': joint,
                        'target_degrees': math.degrees(target),
                        'measured_degrees': math.degrees(state['positions'][key]),
                        'tolerance_degrees': tolerance, 'timestamp': state['timestamp']}
            time.sleep(0.03)
        raise RuntimeError('Target not confirmed within 1.5 degrees before timeout. No retry sent; driver may still be active.')

    def stop(self, *, workflow_request=None):
        topic = f'{self.client.mqtt.topic_prefix}cyberwave/twin/{self.twin}/command'
        payload = {'command': 'stop'}
        if workflow_request is not None:
            payload['workflow_request'] = workflow_request
        if not self.client.mqtt.publish(topic, payload, qos=0):
            raise RuntimeError('Stop publish rejected locally.')
        time.sleep(0.2)
        return {'status': 'stop_requested', 'robot_acknowledged': False,
                'note': 'Driver stop preserves holding torque; this is not a power cut.'}

    def move_pose(self, degrees, *, timeout=60, cancelled=None):
        """Replay is withheld until continuous motion and travel are validated."""
        raise RuntimeError('Saved-pose replay is withheld pending continuous-motion and travel-path validation.')


def configured_channel():
    from dotenv import load_dotenv
    from cyberwave import Cyberwave
    load_dotenv(ROOT / '.env', override=False)
    config = Path(os.getenv('CYBERWAVE_EDGE_CONFIG_DIR', str(Path.home() / '.cyberwave'))).expanduser()
    workspace = os.getenv('CYBERWAVE_WORKSPACE_ID')
    if not workspace:
        workspace = json.loads((config / 'environment.json').read_text())['workspace_uuid']
    key = os.getenv('CYBERWAVE_API_KEY')
    if not key:
        key = json.loads((config / 'credentials.json').read_text())['token']
    twin = os.getenv('CYBERWAVE_TWIN_UUID', '')
    uuid.UUID(twin)
    client = Cyberwave(api_key=key, mode='live', workspace_id=workspace)
    return Channel(client, twin)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--timeout', type=float, default=8, help='Feedback/probe wait in seconds (1..60)')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('status', help='Check cloud access, MQTT round trip and physical feedback without motion')
    sub.add_parser('read', help='Read fresh physical positions in degrees')
    sub.add_parser('start', help='Start existing paired driver remote operation; requires connected robot')
    sub.add_parser('stop', help='Stop remote operation via existing driver; not an emergency power cut')
    sub.add_parser('free', help='Release motor torque for manual positioning. SUPPORT THE ARM FIRST.')
    for name in ('save-pose', 'pose'):
        p = sub.add_parser(name, help='Save current measured angles without motion' if name == 'save-pose' else 'Inspect a saved six-joint pose with --preview')
        p.add_argument('name')
        if name == 'pose':
            p.add_argument('--preview', action='store_true', help='Show saved target without motor access')
    say = sub.add_parser('say', help='Interpret a typed phrase with Grok and execute through Cyberwave')
    say.add_argument('text')
    say.add_argument('--preview', action='store_true', help='Interpret only; never connect to robot')
    sub.add_parser('voice', help='Enter to record -> Whisper -> Grok -> Cyberwave execution')
    sub.add_parser('terminal', help='Typed servo-1 taught-position commands through Cyberwave')
    mapping = sub.add_parser('map-direction', help='Save an operator-observed direction sign; does not move robot')
    mapping.add_argument('joint', choices=JOINTS)
    mapping.add_argument('direction')
    mapping.add_argument('sign', type=int, choices=(-1, 1))
    mapping.add_argument('--viewpoint', required=True)
    for name in ('move', 'jog'):
        p = sub.add_parser(name, help='Absolute target in degrees' if name == 'move' else 'Signed relative change in degrees')
        p.add_argument('joint', choices=JOINTS)
        p.add_argument('degrees', type=float)
    args = parser.parse_args()
    if not finite(args.timeout) or not 1 <= args.timeout <= 60:
        parser.error('--timeout must be between 1 and 60 seconds')
    if hasattr(args, 'degrees') and not finite(args.degrees):
        parser.error('degrees must be finite')
    channel = None
    try:
        native_state = ROOT / '.cache/cyberwave-native/activation.json'
        if args.command in {'start','move','jog'} and native_state.exists():
            if json.loads(native_state.read_text()).get('stage') == 'native_started':
                raise RuntimeError('Native Cyberwave requires a managed movement request. Use ./robot voice or ./robot say with an explicit joint command.')
        if args.command == 'pose' and args.preview:
            from dotenv import load_dotenv
            load_dotenv(ROOT / '.env', override=False)
            import poses
            print(json.dumps({'pose': args.name, 'degrees': poses.load_pose(args.name, os.getenv('CYBERWAVE_TWIN_UUID', '')), 'motion_sent': False}, indent=2))
            return 0
        if args.command == 'pose':
            raise RuntimeError('Saved-pose replay is withheld pending continuous-motion and travel-path validation.')
        if args.command in {'say', 'voice', 'map-direction', 'terminal'}:
            from dotenv import load_dotenv
            load_dotenv(ROOT / '.env', override=False)
            if args.command == 'terminal':
                import base_console
                return base_console.run()
            import voice_control
            if args.command == 'voice':
                import voice_toggle
                return voice_toggle.run(terminal=True, execute=True)
            result = (voice_control.process_text(args.text, preview=args.preview, timeout=args.timeout)
                      if args.command == 'say' else
                      voice_control.save_direction(args.joint, args.direction, args.sign, args.viewpoint))
            print(json.dumps(result, indent=2))
            return int(result['status'] in {'blocked', 'execution_failed', 'interpretation_failed'})
        channel = configured_channel()
        # Resolve authenticated twin before any MQTT command.
        twin = channel.client.twins.get(channel.twin)
        channel.connect()
        if args.command == 'status':
            channel.probe(args.timeout)
            try:
                state = channel.wait_state(args.timeout)
            except RuntimeError:
                state = None
            result = {'backend': 'connected', 'mqtt_round_trip': 'passed',
                      'twin': channel.twin, 'name': twin.name,
                      'robot': 'fresh_feedback' if state else 'offline_or_driver_inactive',
                      'motion_sent': False}
        elif args.command in {'start', 'stop', 'free'}:
            command = {'start': 'remoteoperate', 'stop': 'stop', 'free': 'freedrive'}[args.command]
            # Match the official driver's command envelope; QoS 0, non-retained.
            topic = f'{channel.client.mqtt.topic_prefix}cyberwave/twin/{channel.twin}/command'
            if not channel.client.mqtt.publish(topic, {'command': command, 'timestamp': time.time()}, qos=0):
                raise RuntimeError('Driver command publish rejected locally.')
            if args.command == 'start':
                state = channel.wait_state(args.timeout)
                result = {'status': 'fresh_physical_feedback', 'timestamp': state['timestamp']}
            elif args.command == 'free':
                time.sleep(0.2)
                result = {'status': 'freedrive_requested', 'robot_acknowledged': False,
                          'note': 'Support the arm: freedrive releases torque and the arm can fall. A subsequent explicit voice/say movement request rearms managed control.'}
            else:
                time.sleep(0.2)
                result = {'status': 'stop_requested', 'robot_acknowledged': False,
                          'note': 'Patched driver preserves holding torque. This is not a power cut.'}
        elif args.command == 'save-pose':
            import poses
            result = poses.save_pose(channel, args.name, args.timeout)
        elif args.command == 'read':
            state = channel.wait_state(args.timeout)
            result = {'source': 'edge_follower', 'timestamp': state['timestamp'],
                      'session_id': state['session_id'],
                      'degrees': {n: math.degrees(state['positions'][j]) for n, j in JOINTS.items()}}
        else:
            result = channel.move(args.joint, args.degrees, relative=args.command == 'jog', timeout=args.timeout)
        print(json.dumps(result, indent=2))
        return 0
    except KeyboardInterrupt:
        print(json.dumps({'status': 'interrupted', 'note': 'CLI closed; a previously dispatched target may remain active. Use ./robot stop.'}))
        return 130
    except Exception as exc:
        # Avoid printing HTTP exception bodies/headers that could include credentials.
        detail = str(exc) if isinstance(exc, (RuntimeError, ValueError, FileNotFoundError)) else type(exc).__name__
        print(json.dumps({'status': 'error', 'detail': detail}))
        return 1
    finally:
        if channel:
            channel.client.disconnect()


if __name__ == '__main__':
    raise SystemExit(main())
