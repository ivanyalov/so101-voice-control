"""Validated Grok intent -> verified direction signs -> existing Cyberwave channel."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import threading
import time

import grok_planner
import robot
from turn_log import TurnLog

PAIRS = {
    'left': 'right', 'right': 'left', 'up': 'down', 'down': 'up',
    'forward': 'backward', 'backward': 'forward',
    'clockwise': 'counterclockwise', 'counterclockwise': 'clockwise',
    'open': 'close', 'close': 'open',
}


def mapping_path():
    return robot.ROOT / '.cache/robot/directions.json'


def save_direction(joint, direction, sign, viewpoint):
    if joint not in grok_planner.DIRECTIONS or direction not in grok_planner.DIRECTIONS[joint] or direction not in PAIRS:
        raise ValueError('Direction is not supported for this joint.')
    if type(sign) is not int or sign not in (-1, 1) or not viewpoint.strip():
        raise ValueError('A verified +1/-1 sign and physical viewpoint are required.')
    twin = os.getenv('CYBERWAVE_TWIN_UUID', '')
    if not twin:
        raise ValueError('CYBERWAVE_TWIN_UUID is missing.')
    path = mapping_path()
    data = json.loads(path.read_text()) if path.exists() else {'twin': twin, 'joints': {}}
    if data.get('twin') != twin:
        raise ValueError('Direction profile belongs to a different twin.')
    previous = data['joints'].get(joint, {})
    signs = previous.get('signs', {}) if previous.get('viewpoint') == viewpoint.strip() else {}
    data['joints'][joint] = {**previous, 'verified': True, 'viewpoint': viewpoint.strip(),
                             'signs': {**signs, direction: sign, PAIRS[direction]: -sign},
                             'verified_at': time.time()}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + '\n')
    path.chmod(0o600)
    return {'status': 'direction_saved', 'joint': joint, **data['joints'][joint]}


def resolve_actions(intent, profile, twin):
    """Validate the entire sequence before making any robot connection."""
    paired = any('arm' in a for a in intent.get('actions', []))
    intent = grok_planner.parse_intent(json.dumps(intent), paired=paired)
    if intent['status'] != 'planned':
        return []
    resolved = []
    for action in intent['actions']:
        if action.get('arm', 'follower') != 'follower':
            raise ValueError('Only the paired follower can execute; leader/both are unsupported.')
        if action['type'] == 'stop':
            resolved.append({'type': 'stop'})
            continue
        if action['type'] == 'pose':
            raise ValueError('Saved-pose replay is withheld pending continuous-motion and travel-path validation.')
        if action['type'] == 'opening':
            resolved.append({'type':'absolute', 'joint':'gripper',
                             'target_percent':action['amount_percent'], 'label':'gripper opening'})
            continue
        if action['type'] in {'endpoint', 'fraction'}:
            joint, direction = action['joint'], action['direction']
            settings = profile.get('joints', {}).get(joint, {})
            target = settings.get('targets_degrees', {}).get(direction)
            if (joint != 'shoulder_pan' or profile.get('twin') != twin or
                    settings.get('verified') is not True or not robot.finite(target)):
                raise ValueError(f'No verified taught target for {joint}/{direction} on this twin.')
            target *= action['fraction'] if action['type'] == 'fraction' else 1
            resolved.append({'type': 'absolute', 'joint': joint, 'target_degrees': target,
                             'target_radians': math.radians(target), 'direction': direction,
                             'label': (f"{action['fraction']} × {direction}" if action['type'] == 'fraction' else direction)})
            continue
        if action['type'] != 'relative':
            raise ValueError('Specify a taught position or an explicit degree movement.')
        joint, direction = action['joint'], action['direction']
        if direction in {'positive', 'negative'}:
            resolved.append({'type': 'relative', 'joint': joint,
                             'delta_degrees': action['amount_degrees'] * (1 if direction == 'positive' else -1),
                             'direction': direction, 'viewpoint': 'Explicit signed joint coordinates'})
            continue
        settings = profile.get('joints', {}).get(joint, {})
        sign = settings.get('signs', {}).get(direction)
        if (profile.get('twin') != twin or settings.get('verified') is not True or
                not settings.get('viewpoint') or type(sign) is not int or sign not in (-1, 1)):
            raise ValueError(f'Physical direction {joint}/{direction} is not verified for this twin. Observe a signed jog and save its direction first.')
        resolved.append({'type': 'relative', 'joint': joint,
                         'delta_degrees': sign * action['amount_degrees'],
                         'direction': direction, 'viewpoint': settings['viewpoint']})
    for action, command in zip(intent['actions'], resolved):
        if 'group' in action:
            command['group'] = action['group']
    return resolved


def segment_groups(commands):
    groups, previous = [], None
    for index, command in enumerate(commands):
        group = command.get('group', index)
        if group != previous:
            groups.append([])
            previous = group
        groups[-1].append(command)
    return groups


def load_profile():
    path = mapping_path()
    return json.loads(path.read_text()) if path.exists() else {}


def prepare_intent(intent, *, profile=None, twin=None):
    """One offline boundary shared by typed, microphone and browser requests."""
    started = time.perf_counter()
    result = {'mode': 'prepared', 'status': intent.get('status'), 'reply': intent.get('reply'),
              'proposed_actions': intent.get('actions', []), 'resolved_commands': [],
              'executed_actions': 0, 'published_actions': 0, 'completed_actions': 0, 'acknowledged_actions': 0,
              'results': [], 'motion_sent': False, 'robot_connected': False}
    try:
        profile = load_profile() if profile is None else profile
        twin = os.getenv('CYBERWAVE_TWIN_UUID', '') if twin is None else twin
        commands = resolve_actions(intent, profile, twin)
        result['resolved_commands'] = commands
        smooth = bool(commands) and all(c['type'] in {'absolute', 'relative'} and
            c['joint'] == 'shoulder_pan' for c in commands) and bool(
                profile.get('joints', {}).get('shoulder_pan', {}).get('targets_degrees')) and not any('group' in c for c in commands)
        if smooth:
            import base_console
            from workflow_protocol import PROTOCOL, wire_command
            limits = base_console.validate_commands(commands, profile, twin)
            result.update(execution_format=PROTOCOL, local_trajectory_hz=30,
                prepared_commands=[wire_command(c) for c in commands],
                transport_requirements={'protocol': PROTOCOL, 'units': 'radians',
                    'capabilities': ['persistent_freedrive'],
                    'limits_radians': limits, 'speed_degrees_s': 45,
                    'start_source': 'fresh driver measurement at segment acceptance',
                    'requires': ['correlated remoteoperate readiness', 'fresh complete feedback', 'supported folded pose'],
                    'driver_patch_required': True})
        elif commands and all(c['type'] in {'absolute','relative'} for c in commands):
            from workflow_protocol import COORDINATED, coordinated_command
            groups = segment_groups(commands)
            result.update(execution_format=COORDINATED, local_trajectory_hz=30,
                resolved_segments=groups, prepared_commands=[coordinated_command(g) for g in groups],
                transport_requirements={'protocol':'so101.workflow.v1',
                    'capabilities':['persistent_freedrive',COORDINATED],
                    'units':{'arm':'radians','gripper':'percent'},
                    'start_source':'fresh driver measurement',
                    'limits_source':'driver calibration and taught base bounds',
                    'driver_patch_required':True,
                    'completion':'all six measured joints',
                    'final_mode':'freedrive when folded, hold otherwise'})
        else:
            result.update(execution_format='existing_single_joint',
                          prepared_commands=[({'type': 'stop'} if c['type'] == 'stop' else {
                              'type': 'relative', 'joint': robot.JOINTS[c['joint']],
                              'delta_radians': math.radians(c['delta_degrees'])}) for c in commands],
                          transport_requirements={
                              'start_source': 'fresh channel measurement', 'units': 'radians'})
        result['dispatch_state'] = 'prepared' if commands else 'no_actions'
    except (ValueError, OSError, KeyError, TypeError) as exc:
        result.update(status='blocked', detail=str(exc), dispatch_state='blocked_before_dispatch')
    result['preparation_elapsed_seconds'] = round(time.perf_counter()-started, 3)
    return result


def execute_intent(intent, *, profile=None, channel_factory=None, timeout=15, cancelled=None,
                   prepared=None):
    cancelled = cancelled or threading.Event()
    try:
        profile = load_profile() if profile is None else profile
    except (OSError, ValueError) as exc:
        return {**prepare_intent(intent, profile={}), 'status': 'blocked', 'detail': str(exc)}
    result = dict(prepared if prepared is not None else prepare_intent(intent, profile=profile))
    result['mode'] = 'cyberwave_live'
    commands = result['resolved_commands']
    result['transport'] = 'cyberwave_mqtt'
    result.update(connection_preflight_elapsed_seconds=0., execution_confirmation_elapsed_seconds=0.)
    if result['status'] == 'blocked' or not commands:
        return result
    channel = None
    stage, stage_started = 'connection_preflight', time.perf_counter()
    def next_stage(name):
        nonlocal stage, stage_started
        result[stage+'_elapsed_seconds'] += round(time.perf_counter()-stage_started, 3)
        stage, stage_started = name, time.perf_counter()
        result['stage'] = name
        print('Execution: waiting for driver acceptance and measured completion…', flush=True)
    try:
        if cancelled.is_set():
            result.update(status='cancelled', dispatch_state='blocked_before_dispatch')
            return result
        result['stage'] = stage
        print('Connection/preflight: connecting to Cyberwave…', flush=True)
        channel = (channel_factory or robot.configured_channel)()
        channel.client.twins.get(channel.twin)
        channel.connect()
        result['robot_connected'] = True
        if channel.twin != os.getenv('CYBERWAVE_TWIN_UUID', ''):
            raise RuntimeError('Connected twin differs from the configured target.')
        if result.get('execution_format') == 'so101.workflow.v1':
            import base_console
            def completed(segment):
                result['results'].append(segment)
                result['completed_actions'] = len(result['results'])
                result['executed_actions'] = channel.dispatched
            def accepted(segment):
                result['acknowledged_actions'] += 1
            measured = base_console.run_targets(channel, commands, profile=profile, cancelled=cancelled,
                on_complete=completed, on_accepted=accepted, on_stage=lambda _: next_stage('execution_confirmation'))
            result.update(status='completed', results=measured,
                          executed_actions=channel.dispatched, completed_actions=len(measured))
            return result
        if result.get('execution_format') == 'coordinated_joints_v1':
            import smooth_motion
            def completed(segment):
                result['results'].append(segment)
                result['completed_actions'] = len(result['results'])
                result['executed_actions'] = channel.dispatched
            def accepted(segment):
                result['acknowledged_actions'] += 1
            measured, final_mode = smooth_motion.run_segments(channel, result['prepared_commands'],
                profile=profile, cancelled=cancelled, on_complete=completed, on_accepted=accepted,
                on_stage=lambda _:next_stage('execution_confirmation'))
            result.update(status='completed', results=measured, final_mode=final_mode,
                executed_actions=channel.dispatched, completed_actions=len(measured))
            return result
        next_stage('execution_confirmation')
        if len(commands)!=1 or commands[0]['type']!='stop':
            raise RuntimeError('Movement requires the versioned smooth workflow.')
        result['results'].append(channel.stop())
        result['status'] = 'stop_requested'
        return result
    except Exception as exc:
        count = channel.dispatched if channel else 0
        result.update(status='execution_failed', executed_actions=count,
            driver_rejected=getattr(exc, 'driver_rejected', False),
            detail=str(exc) if isinstance(exc, (RuntimeError, ValueError)) else type(exc).__name__,
            note=('No motion target published.' if count == 0 else
                  'No retry or later action sent. Published targets require measured confirmation.'),
            cleanup_errors=getattr(exc, '__notes__', []))
        return result
    finally:
        result[stage+'_elapsed_seconds'] += round(time.perf_counter()-stage_started, 3)
        result['executed_actions'] = channel.dispatched if channel else 0
        result['published_actions'] = result['executed_actions']
        result['motion_sent'] = result['executed_actions'] > 0
        result['dispatch_state'] = ('measured_complete' if result['status'] == 'completed' else
            'rejected_by_driver' if result.get('driver_rejected') and result['motion_sent'] else
            'uncertain_after_dispatch' if result['motion_sent'] else 'blocked_before_dispatch')
        if channel:
            try:
                channel.client.disconnect()
            except Exception as exc:
                result.setdefault('cleanup_errors', []).append('Disconnect: '+type(exc).__name__)


def process_text(text, *, preview=False, timeout=15, cancelled=None):
    started = time.perf_counter()
    print('Interpretation: waiting for Grok…', flush=True)
    try:
        intent = grok_planner.interpret(text, follower_only=True)
    except (RuntimeError, ValueError) as exc:
        result = {'mode': 'cyberwave_preview' if preview else 'cyberwave_live',
            'status': 'interpretation_failed', 'stage': 'interpretation', 'detail': str(exc),
            'executed_actions': 0, 'published_actions': 0, 'completed_actions': 0,
            'acknowledged_actions': 0, 'motion_sent': False, 'robot_connected': False,
            'dispatch_state': 'blocked_before_dispatch',
            'interpretation_elapsed_seconds': round(time.perf_counter()-started, 3)}
        TurnLog(robot.ROOT / '.cache/logs' / f'robot-intent-{time.time_ns()}.jsonl').write('intent_result', text=text, result=result)
        return result
    interpretation = round(time.perf_counter()-started, 3)
    print('Preparation: resolving exact commands…', flush=True)
    try:
        profile = load_profile()
    except (ValueError, OSError) as exc:
        result = prepare_intent(intent, profile={})
        result.update(status='blocked', detail=str(exc), dispatch_state='blocked_before_dispatch')
        profile = {}
    else:
        result = prepare_intent(intent, profile=profile)
    if preview:
        result['mode'] = 'cyberwave_preview'
    elif result['status'] != 'blocked':
        result = execute_intent(intent, profile=profile, prepared=result, timeout=timeout, cancelled=cancelled)
    result['interpretation_elapsed_seconds'] = interpretation
    result['processing_elapsed_seconds'] = round(time.perf_counter()-started, 3)
    log = TurnLog(robot.ROOT / '.cache/logs' / f'robot-intent-{time.time_ns()}.jsonl')
    log.write('intent_result', text=text, result=result)
    return result
