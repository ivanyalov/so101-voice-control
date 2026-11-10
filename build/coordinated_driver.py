"""Six-joint adapter inside the existing Cyberwave workflow and motor writer."""
import math

try:
    from utils.cw_workflow_protocol import (COORDINATED, JOINTS, finite,
        coordinated_trajectory, validate_specs)
    from utils.cw_local_trajectory import schedule_chunks
    from so101.session_hold import verify_calibration, hold_current, read_complete
except ImportError:
    from workflow_protocol import (COORDINATED, JOINTS, finite,
        coordinated_trajectory, validate_specs)
    from cyberwave_trajectory import schedule_chunks
    from cyberwave_hold import verify_calibration, hold_current, read_complete


def require_collision_model(follower):
    if (any(getattr(follower,attr,None) is None for attr in
            ('_pin_model','_pin_data','_pin_collision_model','_pin_collision_data','_pin_floor_geom_id'))
            or not callable(getattr(follower,'validate_action_kinematics',None))):
        raise ValueError('Coordinated movement requires driver self-collision and floor geometry.')


class Coordinated:
    def __init__(self, workflow):
        self.w = workflow
        self.specs = None
        self.target = None
        self.finished = False
        self.folded = None

    def specifications(self, base_limits):
        w = self.w
        verify_calibration(w.follower)
        require_collision_model(w.follower)
        specs = {}
        for name, key in JOINTS.items():
            mode = w.norm_modes[name]
            if getattr(mode, 'value', mode) != ('range_0_100' if name=='gripper' else 'range_m100_100'):
                raise ValueError('Unreviewed coordinated joint normalization: '+name)
            calibration = w.calibration.get(name)
            if (calibration is None or not finite(calibration.range_min)
                    or not finite(calibration.range_max) or not 0 <= calibration.range_min < calibration.range_max <= 4095
                    or any(getattr(calibration,field,None)!=getattr(w.follower.calibration.get(name),field,None)
                           for field in ('id','homing_offset','range_min','range_max'))):
                raise ValueError('Matching driver calibration required: '+name)
            if name == 'gripper':
                spec = dict(unit='percent', limits=[0,100], peak_speed=45, tolerance=2)
            else:
                limits = [w.to_rad(x, mode, calibration) for x in (-100,100)]
                if name == 'shoulder_pan' and base_limits is not None:
                    if (not isinstance(base_limits, list) or len(base_limits)!=2
                            or not all(finite(x) for x in base_limits) or base_limits[0]>=base_limits[1]):
                        raise ValueError('Invalid taught base bounds.')
                    limits = [max(limits[0], base_limits[0]), min(limits[1], base_limits[1])]
                spec = dict(unit='radians', limits=limits, peak_speed=math.radians(45), tolerance=math.radians(1.5))
            specs[key] = spec
        return validate_specs(specs)

    def coordinates(self, obs):
        w = self.w
        values = {key:obs[name] for name,key in JOINTS.items()}
        values['_6'] = w.from_rad(obs['gripper'], w.norm_modes['gripper'], w.calibration['gripper'])
        if not all(finite(x) for x in values.values()):
            raise ValueError('Incomplete calibrated joint observation.')
        return values

    def confirm(self, data, obs):
        w = self.w
        if self.finished or (w.released and not w.awaiting_confirm):
            raise ValueError('Finished operation cannot enable torque again.')
        if w.queue.unfinished_tasks:
            raise ValueError('Writer is not idle; cannot confirm control.')
        specs = self.specifications(data.get('base_limits_radians'))
        # Check the fresh start against driver bounds before any rearming.
        start = self.coordinates(obs)
        for key,spec in specs.items():
            if not spec['limits'][0] <= start[key] <= spec['limits'][1]:
                raise ValueError('Current joint lies outside calibrated/taught bounds: '+key)
        if w.awaiting_confirm:
            try:
                hold_current(w.follower)
                if w.released:
                    w.follower.enable_torque()
                if any(x!=1 for x in read_complete(w.follower,'Torque_Enable').values()):
                    raise ValueError('Powered-mode readback failed.')
            except (RuntimeError, ValueError, OSError):
                w.stop_event.set()
                raise
            w.released, w.awaiting_confirm = False, False
        elif not w.follower.torque_enabled:
            raise ValueError('Remoteoperate is not powered.')
        if any(x!=1 for x in read_complete(w.follower,'Torque_Enable').values()):
            raise ValueError('Powered-mode readback failed.')
        self.specs = specs
        folded = data.get('supported_pose_radians')
        others = set(JOINTS)-{'shoulder_pan'}
        self.folded = dict(folded) if (isinstance(folded,dict) and set(folded)==others
            and all(finite(v) for v in folded.values())) else None
        gripper_rad = [w.to_rad(v,w.norm_modes['gripper'],w.calibration['gripper']) for v in (0,100)]
        return dict(status='ready', capabilities=['persistent_freedrive', COORDINATED], joint_specs=specs,
                    gripper_radians=gripper_rad)

    def segment(self, data, obs):
        w = self.w
        if self.finished or self.specs is None or w.released or w.awaiting_confirm:
            raise ValueError('New operation confirmation required before coordinated motion.')
        if w.queue.unfinished_tasks:
            raise ValueError('Previous trajectory is still running.')
        require_collision_model(w.follower)
        verify_calibration(w.follower)
        values, duration = coordinated_trajectory(self.coordinates(obs), data['command'], self.specs)
        schedule = schedule_chunks({'local_trajectory_hz':30}, len(values))
        actions = []
        for values_at_time, meta in zip(values, schedule):
            action = {}
            for name,key in JOINTS.items():
                value, mode, calibration = values_at_time[key], w.norm_modes[name], w.calibration[name]
                norm = value if name=='gripper' else w.from_rad(value,mode,calibration)
                valid,_ = w.validate(w.follower.motors[name].id,norm)
                converted = norm if name=='gripper' else w.to_rad(norm,mode,calibration)
                if not finite(norm) or not valid or not math.isclose(converted,value,rel_tol=0,abs_tol=1e-9):
                    raise ValueError('Driver position limit rejected joint: '+name)
                action[name+'.pos'] = norm
            safe,_ = w.follower.validate_action_kinematics(action,trigger_callback=False)
            if not safe:
                raise ValueError('Driver collision check rejected coordinated trajectory.')
            actions.append({**action, '_delta_meta':{**meta,'workflow_v1':True,
                'coordinated_v1':True,'inter_chunk_delay_sec':0}})
        # One complete six-joint action per tick; the existing writer also checks
        # collisions and load. Never enqueue a partly validated trajectory.
        with w.queue.mutex:
            if w.stop_event.is_set() or w.queue.unfinished_tasks:
                raise ValueError('Writer stopped or has pending commands.')
            if w.queue.maxsize>0 and len(actions)>w.queue.maxsize:
                raise ValueError('Insufficient writer queue capacity.')
            w.queue.queue.extend(actions)
            w.queue.unfinished_tasks += len(actions)
            w.queue.not_empty.notify()
        self.target = dict(values[-1])
        return dict(status='accepted', start_coordinates=values[0], target_coordinates=values[-1],
                    duration_seconds=duration, units={key:s['unit'] for key,s in self.specs.items()})

    def finish(self, obs):
        w = self.w
        if self.target is None or w.queue.unfinished_tasks:
            raise ValueError('No completed idle coordinated trajectory.')
        measured = self.coordinates(obs)
        if any(abs(measured[key]-value)>self.specs[key]['tolerance'] for key,value in self.target.items()):
            raise ValueError('Six-joint endpoint not measured; retaining holding torque.')
        if not self.finished:
            folded = self.folded is not None and all(abs(obs[n]-v)<=math.radians(2) for n,v in self.folded.items())
            try:
                if folded:
                    w.follower.disable_torque()
                elif not w.follower.torque_enabled:
                    raise ValueError('Holding torque lost before completion.')
                if any(v != (0 if folded else 1) for v in read_complete(w.follower,'Torque_Enable').values()):
                    raise ValueError('Final torque-mode readback failed.')
            except (RuntimeError,ValueError,OSError):
                w.stop_event.set()
                raise
            w.released = folded
            self.finished = True
        if any(v != (0 if w.released else 1) for v in read_complete(w.follower,'Torque_Enable').values()):
            w.stop_event.set()
            raise ValueError('Final torque mode changed.')
        return dict(status='released' if w.released else 'held',
                    mode='freedrive' if w.released else 'remoteoperate',
                    final_mode='freedrive' if w.released else 'hold')
