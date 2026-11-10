"""Shared coordinated voice executor over the existing Cyberwave workflow."""
import math
import threading
import time

import base_console
import poses
import robot
from workflow_client import WorkflowClient
from workflow_protocol import (COORDINATED, HZ,
    coordinated_target, validate_specs)


def run_segments(channel, commands, *, profile, cancelled=None, on_complete=None,
                 on_accepted=None, on_stage=None):
    cancelled = cancelled or threading.Event()
    workflow, requested, results = None, False, []

    def check():
        if cancelled.is_set():
            raise RuntimeError('Command cancelled; no later segment sent.')
        state = channel.snapshot()
        if not channel.client.mqtt.connected:
            raise RuntimeError('Cyberwave connection lost; no later segment sent.')
        return state

    def wait_state(timeout, session=None, newer_than=None):
        deadline = time.monotonic()+timeout
        while time.monotonic()<deadline:
            state = check()
            if (state and (session is None or state['session_id']==session)
                    and (newer_than is None or state['timestamp']>newer_than)):
                return state
            time.sleep(.02)
        raise RuntimeError('No fresh six-joint physical feedback; no later segment sent.')

    def coordinates(state):
        values = dict(state['positions'])
        # Cyberwave telemetry remains calibrated radians for all six motors;
        # only the command/specification interface exposes gripper percentage.
        values['_6'] = (values['_6']-gripper_rad[0])*100/(gripper_rad[1]-gripper_rad[0])
        return values

    try:
        print('Preflight: waiting for fresh six-joint feedback…',flush=True)
        wait_state(15)
        supported = None
        try:
            folded = poses.load_pose('start',channel.twin)
            supported = {n:math.radians(v) for n,v in folded.items() if n!='shoulder_pan'}
        except ValueError:
            pass  # Without a verified supported pose, the driver retains holding torque.
        base_limits = None
        targets = profile.get('joints',{}).get('shoulder_pan',{}).get('targets_degrees',{})
        if targets:
            if profile.get('twin')!=channel.twin:
                raise ValueError('Taught profile belongs to another twin.')
            base_limits = base_console.validate_commands([],profile,channel.twin)
        workflow = WorkflowClient(channel,cancelled)
        requested, began = True, time.time()
        base_console.command(channel,'remoteoperate',workflow_request=workflow.operation_id)
        print('Preflight: waiting for coordinated-driver readiness…',flush=True)
        ready = workflow.exchange('confirm',timeout=35,retry=True,motion_profile=COORDINATED,
            supported_pose_radians=supported,base_limits_radians=base_limits)
        session, specs = ready['session_id'], validate_specs(ready.get('joint_specs'))
        gripper_rad = ready.get('gripper_radians')
        if (not isinstance(gripper_rad,list) or len(gripper_rad)!=2
                or not all(robot.finite(v) for v in gripper_rad) or gripper_rad[0]!=0
                or not 0<gripper_rad[1]<=2*math.pi):
            raise RuntimeError('Invalid driver gripper calibration.')
        state = wait_state(8,session,began)
        # Validate the entire sequence against one fresh preflight snapshot.
        # The driver re-resolves each relative group from its own fresh start.
        projected = coordinates(state)
        for command in commands:
            projected,_ = coordinated_target(projected,command,specs)
        if on_stage:
            on_stage('execution')
        for command in commands:
            state = check()
            if state is None or state['session_id']!=session:
                raise RuntimeError('Fresh feedback/session lost before dispatch.')
            coordinated_target(coordinates(state),command,specs)
            def published():
                channel.dispatched += 1
            accepted = workflow.exchange('segment',timeout=5,session_id=session,
                command=command,on_publish=published)
            start, target = accepted.get('start_coordinates'), accepted.get('target_coordinates')
            expected, steps = coordinated_target(start,command,specs)
            duration = accepted.get('duration_seconds')
            if (not isinstance(target,dict) or set(target)!=set(specs)
                    or not all(robot.finite(target[k]) and math.isclose(target[k],expected[k],abs_tol=1e-10,rel_tol=0) for k in specs)
                    or not robot.finite(duration) or not math.isclose(duration,steps/HZ,abs_tol=1e-10,rel_tol=0)
                    or accepted.get('units')!={k:s['unit'] for k,s in specs.items()}):
                raise RuntimeError('Invalid coordinated trajectory acknowledgement.')
            if on_accepted:
                on_accepted(accepted)
            print('Execution: coordinated local 30 Hz movement; waiting for all six joints…',flush=True)
            deadline, last, consecutive = time.monotonic()+duration+10, None, 0
            while time.monotonic()<deadline:
                state = check()
                if state is None or state['session_id']!=session:
                    raise RuntimeError('Fresh feedback/session lost during coordinated motion.')
                measured = coordinates(state)
                for key,spec in specs.items():
                    low,high = spec['limits']
                    if not low-spec['tolerance']<=measured[key]<=high+spec['tolerance']:
                        raise RuntimeError('Joint left calibrated/taught range: '+key)
                if state['timestamp']!=last:
                    last = state['timestamp']
                    within = all(abs(measured[k]-target[k])<=specs[k]['tolerance'] for k in specs)
                    consecutive = consecutive+1 if last>accepted['timestamp']+duration and within else 0
                    if consecutive>=3:
                        result = dict(target_coordinates=target, measured_coordinates=measured,
                            units=accepted['units'],timestamp=last,duration_seconds=duration,
                            participating_joints=list(command['joints']))
                        results.append(result)
                        if on_complete:
                            on_complete(result)
                        break
                time.sleep(.02)
            else:
                raise RuntimeError('All-joint endpoint not confirmed; no later segment sent.')
        finished = workflow.exchange('finish',timeout=5,session_id=session)
        final_mode = finished.get('final_mode')
        if final_mode != ('freedrive' if finished['status']=='released' else 'hold'):
            raise RuntimeError('Invalid final torque-mode acknowledgement.')
        print('Freedrive confirmed in folded pose.' if final_mode=='freedrive' else
              'Holding final pose; connected driver retained.',flush=True)
        return results, final_mode
    except BaseException as exc:
        if requested:
            try:
                channel.stop(workflow_request=workflow.operation_id)
            except Exception as stop_error:
                exc.add_note('Stop/hold request failed: '+type(stop_error).__name__)
        raise
    finally:
        if workflow:
            try:
                workflow.close()
            except Exception:
                pass
