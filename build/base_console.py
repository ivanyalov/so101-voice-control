"""Typed servo-1 commands, taught absolute targets, Cyberwave local trajectory."""
import json
import math
import re
import time
import threading

import poses
import robot
from workflow_client import WorkflowClient
from workflow_protocol import wire_command, validate_segment


def parse(text):
    text = text.lower().strip()
    text = re.sub(r'\b(?:servo|motor|driver)\s*1\b', '', text)
    text = re.sub(r'\b(?:all the way|move|go|to|the|and|then|back|stop at)\b', ' ', text)
    words = text.replace(',', ' ').replace('→', ' ').split()
    if not words or any(x not in {'right', 'left', 'middle'} for x in words):
        raise ValueError('Use right, left, middle, or a sequence such as right then left then middle.')
    return words


def command(channel, name, **fields):
    topic = f'{channel.client.mqtt.topic_prefix}cyberwave/twin/{channel.twin}/command'
    if not channel.client.mqtt.publish(topic, {'command': name, 'timestamp': time.time(), **fields}, qos=0):
        raise RuntimeError('Driver command rejected.')


def run_sequence(channel, labels):
    profile = json.loads((robot.ROOT / '.cache/robot/directions.json').read_text())
    prepared = prepare_sequence(labels, profile, channel.twin)
    if prepared['status'] == 'blocked':
        raise ValueError(prepared['detail'])
    return run_targets(channel, prepared['resolved_commands'], profile=profile)


def prepare_sequence(labels, profile, twin):
    import voice_control
    intent = {'status': 'planned', 'reply': 'Use the taught base positions.', 'actions': [
        dict(type='endpoint', joint='shoulder_pan', direction=label, amount_degrees=None,
             fraction=None, pose=None, arm='follower') for label in labels]}
    return voice_control.prepare_intent(intent, profile=profile, twin=twin)


def validate_commands(commands, profile, twin):
    if profile.get('twin') != twin:
        raise ValueError('Taught targets belong to a different robot.')
    settings = profile.get('joints', {}).get('shoulder_pan', {})
    targets = settings.get('targets_degrees', {})
    if settings.get('verified') is not True or not all(robot.finite(targets.get(k)) for k in ('left', 'right', 'middle')):
        raise ValueError('Incomplete verified taught base targets.')
    low, high = min(targets[k] for k in ('left', 'right', 'middle')), max(targets[k] for k in ('left', 'right', 'middle'))
    if low >= high:
        raise ValueError('Invalid taught base range.')
    limits = [math.radians(low), math.radians(high)]
    for item in commands:
        wire = wire_command(item)
        if wire['type'] == 'absolute':
            validate_segment(limits[0], wire, limits)
    return limits


def run_targets(channel, commands, *, profile, cancelled=None, on_complete=None,
                on_accepted=None, on_stage=None):
    """Shared exact-base executor: one Cyberwave local trajectory per segment."""
    cancelled = cancelled or threading.Event()

    def check_cancelled():
        if cancelled.is_set():
            raise RuntimeError('Command cancelled; no later segment sent.')

    check_cancelled()
    limits = validate_commands(commands, profile, channel.twin)
    low, high = [math.degrees(x) for x in limits]

    def wait_state(timeout, *, session=None, newer_than=None):
        deadline = time.monotonic()+timeout
        while time.monotonic() < deadline:
            check_cancelled()
            if not channel.client.mqtt.connected:
                raise RuntimeError('Cyberwave connection lost; no later segment sent.')
            state = channel.snapshot()
            if (state and (session is None or state['session_id'] == session)
                    and (newer_than is None or state['timestamp'] > newer_than)):
                return state
            time.sleep(.03)
        raise RuntimeError('No fresh six-joint physical feedback; no movement sent.')

    print('Preflight: waiting for fresh physical feedback…', flush=True)
    initial = wait_state(15)
    folded = poses.load_pose('start', channel.twin)
    if any(abs(math.degrees(initial['positions'][key])-folded[name]) > 2
           for name, key in robot.JOINTS.items() if name != 'shoulder_pan'):
        raise RuntimeError('Other joints differ from the supported folded pose; no movement sent.')
    # Resolve/check the complete sequence before enabling power. Relative amounts
    # are also rechecked against fresh measurements at each segment boundary.
    position = math.degrees(initial['positions']['_1'])
    for item in commands:
        if item.get('joint') != 'shoulder_pan' or item.get('type') not in {'absolute', 'relative'}:
            raise ValueError('Smooth taught-position sequence supports the base only.')
        value = item.get('target_degrees' if item['type'] == 'absolute' else 'delta_degrees')
        if not robot.finite(value):
            raise ValueError('Nonfinite base target.')
        target = value if item['type'] == 'absolute' else position + value
        if not low <= target <= high:
            raise ValueError('Requested base target is outside taught travel; no movement sent.')
        validate_segment(math.radians(position), wire_command(item), limits)
        position = target
    check_cancelled()
    if channel.snapshot() is None or not channel.client.mqtt.connected:
        raise RuntimeError('Fresh feedback/connection lost; no movement sent.')
    results = []
    workflow = WorkflowClient(channel, cancelled)
    requested = False
    try:
        requested = True  # A publish exception is also an uncertain control request.
        began = time.time()
        command(channel, 'remoteoperate', workflow_request=workflow.operation_id)
        print('Preflight: waiting for correlated powered-driver readiness…', flush=True)
        supported = {n: math.radians(v) for n, v in folded.items() if n != 'shoulder_pan'}
        ready = workflow.exchange('confirm', timeout=35, retry=True, supported_pose_radians=supported)
        session = ready['session_id']
        state = wait_state(8, session=session, newer_than=began)
        if on_stage:
            on_stage('execution')
        for item in commands:
            check_cancelled()
            before = wait_state(8)
            if before['session_id'] != session:
                raise RuntimeError('Driver session changed; no later segment sent.')
            target = (item['target_degrees'] if item['type'] == 'absolute' else
                      math.degrees(before['positions']['_1']) + item['delta_degrees'])
            if not low <= target <= high:
                raise RuntimeError('Requested base target is outside taught travel.')
            check_cancelled()
            current = channel.snapshot()
            if (not channel.client.mqtt.connected or current is None or
                    current['session_id'] != session):
                raise RuntimeError('Fresh feedback/connection/session lost before dispatch.')
            def published():
                channel.dispatched += 1
            accepted = workflow.exchange('segment', timeout=5, session_id=session,
                command=wire_command(item), limits_radians=limits, speed_degrees_s=45,
                supported_pose_radians=supported,
                on_publish=published)
            duration, authoritative = accepted.get('duration_seconds'), accepted.get('target_radians')
            if (not robot.finite(duration) or not .5 <= duration <= 255/30
                    or not robot.finite(authoritative) or not limits[0] <= authoritative <= limits[1]
                    or (item['type'] == 'absolute' and authoritative != math.radians(item['target_degrees']))):
                raise RuntimeError('Invalid driver trajectory acknowledgement.')
            target = math.degrees(authoritative)
            sent = accepted['timestamp']
            if on_accepted:
                on_accepted(accepted)
            label = item.get('label', item.get('direction', 'base'))
            print(f'{label}: target {target!r}° via Cyberwave local 30 Hz trajectory', flush=True)
            deadline = time.monotonic()+duration+10
            last = None
            consecutive = 0
            while time.monotonic() < deadline:
                check_cancelled()
                state = channel.snapshot()
                if state is None or not channel.client.mqtt.connected or state['session_id'] != session:
                    raise RuntimeError('Fresh feedback/connection lost or driver session changed.')
                measured = math.degrees(state['positions']['_1'])
                if not low-1.5 <= measured <= high+1.5:
                    raise RuntimeError('Base left the taught range.')
                if state['timestamp'] != last:
                    last = state['timestamp']
                    consecutive = consecutive+1 if last > sent+duration and abs(measured-target) <= 1.5 else 0
                    if consecutive >= 3:
                        results.append({'label': label, 'target_degrees': target,
                                        'measured_degrees': measured, 'timestamp': last})
                        if on_complete:
                            on_complete(results[-1])
                        break
                time.sleep(.02)
            else:
                raise RuntimeError('Endpoint not confirmed; no later segment sent.')
        workflow.exchange('release', timeout=5, session_id=session)
        print('Freedrive confirmed; Cyberwave connection retained for the next command.', flush=True)
        return results
    except BaseException as exc:
        if requested:
            try:
                channel.stop(workflow_request=workflow.operation_id)
            except Exception as stop_error:
                exc.add_note('Stop/hold request failed: '+type(stop_error).__name__)
        raise
    finally:
        try:
            workflow.close()
        except Exception:
            pass  # Subscription cleanup must not mask the execution outcome.


def run():
    channel = robot.configured_channel()
    try:
        channel.client.twins.get(channel.twin)
        channel.connect()
        print('SERVO 1 · Cyberwave · taught positions · local 30 Hz smoothstep', flush=True)
        print('right / left / middle / right then left then middle', flush=True)
        print('read / free / stop / quit. Launching this console sends no movement.', flush=True)
        while True:
            try:
                text = input('servo1> ').strip()
                if text in {'quit', 'exit'}:
                    return 0
                if not text:
                    continue
                if text == 'read':
                    state = channel.wait_state(10)
                    print(f"Servo 1: {math.degrees(state['positions']['_1']):.2f}°", flush=True)
                elif text in {'free', 'stop'}:
                    command(channel, 'freedrive' if text == 'free' else 'stop')
                    time.sleep(1)
                    print('Requested ' + text, flush=True)
                else:
                    labels = parse(text)
                    result = run_sequence(channel, labels)
                    path = robot.ROOT / '.cache/logs' / f'base-console-{time.time_ns()}.json'
                    path.write_text(json.dumps({'commands': labels, 'results': result}, indent=2)+'\n')
                    print('Completed; measured feedback confirmed.', flush=True)
            except ValueError as exc:
                print(str(exc), flush=True)
            except KeyboardInterrupt:
                print('\nInterrupted; no further sequence sent.', flush=True)
                return 130
            except (RuntimeError, OSError, KeyError) as exc:
                print(f'Blocked: {exc}', flush=True)
    finally:
        channel.client.disconnect()


if __name__ == '__main__':
    raise SystemExit(run())
