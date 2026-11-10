"""Offline-prepared adapter for the existing Cyberwave remoteoperate writer.

Installed as utils/cw_workflow.py by the versioned driver patch. No standalone
controller and no direct motor writes: validated actions use the existing queue.
"""
import math
import queue
import threading
import time

try:
    from utils.cw_workflow_protocol import PROTOCOL, COORDINATED, finite, topics, trajectory
    from utils.cw_coordinated_driver import Coordinated
    from utils.cw_local_trajectory import schedule_chunks
    from so101.session_hold import hold_current, read_complete, verify_calibration
except ImportError:  # Local offline tests.
    from workflow_protocol import PROTOCOL, COORDINATED, finite, topics, trajectory
    from coordinated_driver import Coordinated
    from cyberwave_trajectory import schedule_chunks
    from cyberwave_hold import hold_current, read_complete, verify_calibration


class Workflow:
    def __init__(self, mqtt, twin, operation_id, follower, stop_event, action_queue,
                 norm_modes, calibration, to_radians, from_radians, validate):
        self.mqtt, self.twin, self.operation_id = mqtt, twin, operation_id
        self.follower, self.stop_event, self.queue = follower, stop_event, action_queue
        self.norm_modes, self.calibration = norm_modes, calibration or {}
        self.to_rad, self.from_rad, self.validate = to_radians, from_radians, validate
        self.request_topic, self.response_topic = topics(mqtt.topic_prefix, twin)
        self.lock = threading.Lock()
        self.seen = set()
        self.released = False
        self.last_target = None
        self.folded = None
        self.awaiting_confirm = False
        self.coordinated = None

    def start(self):
        self.follower._workflow_session = self
        self.mqtt.subscribe(self.request_topic, self.receive)

    def reserve(self, operation_id):
        """Reuse only an idle released loop; this method never enables torque."""
        with self.lock:
            if (not isinstance(operation_id, str) or len(operation_id) != 32
                    or self.stop_event.is_set() or not self.follower.connected):
                return False
            if operation_id == self.operation_id:
                return True
            if (not self.released and not (self.coordinated and self.coordinated.finished)) or self.queue.unfinished_tasks:
                return False
            self.operation_id = operation_id
            self.seen.clear()
            self.last_target = self.folded = None
            self.awaiting_confirm = True
            self.coordinated = None
            return True

    def close(self):
        # Teardown must not wait forever for a stopped writer's pending actions.
        try:
            self.mqtt.unsubscribe(self.request_topic)
        finally:
            if getattr(self.follower, '_workflow_session', None) is self:
                self.follower._workflow_session = None
            while True:
                try:
                    self.queue.get_nowait()
                    self.queue.task_done()
                except queue.Empty:
                    break

    def observation(self, *, require_power=True):
        if (self.stop_event.is_set() or not self.mqtt.connected or not self.follower.connected
                or (require_power and not self.follower.torque_enabled)):
            raise ValueError('Remoteoperate is not ready.')
        # Patched get_observation returns {} on read failure, never its cache.
        obs = self.follower.get_observation()
        if not isinstance(obs, dict) or len(self.follower.motors) != 6 or not all(finite(obs.get(n+'.pos')) for n in self.follower.motors):
            raise ValueError('No fresh complete six-joint driver measurement.')
        return {n: self.to_rad(obs[n+'.pos'], self.norm_modes[n], self.calibration.get(n))
                for n in self.follower.motors}

    def check_folded(self, obs, folded):
        others = set(self.follower.motors) - {'shoulder_pan'}
        if (not isinstance(folded, dict) or set(folded) != others or not all(finite(folded[n]) and
                abs(obs[n]-folded[n]) <= math.radians(2) for n in others)):
            raise ValueError('Other joints differ from the supported folded pose.')

    def confirm(self, data, obs):
        self.check_folded(obs, data.get('supported_pose_radians', {}))
        if self.released:
            if not self.awaiting_confirm:
                raise ValueError('Finished operation cannot enable torque again.')
            if self.queue.unfinished_tasks:
                raise ValueError('Writer is not idle; cannot enable control.')
            try:
                verify_calibration(self.follower)
                hold_current(self.follower)  # Replace stale goals before enabling torque.
                self.follower.enable_torque()
                if any(value != 1 for value in read_complete(self.follower, 'Torque_Enable').values()):
                    raise ValueError('Powered-mode readback failed.')
            except (RuntimeError, ValueError, OSError):
                self.stop_event.set()
                raise
            self.released = False
            self.awaiting_confirm = False
        elif not self.follower.torque_enabled:
            raise ValueError('Remoteoperate is not powered.')

    def release(self, obs):
        if not self.released:
            if self.queue.unfinished_tasks or self.last_target is None:
                raise ValueError('No completed idle trajectory to release.')
            self.check_folded(obs, self.folded)
            if abs(obs['shoulder_pan']-self.last_target) > math.radians(1.5):
                raise ValueError('Endpoint not measured; retaining holding torque.')
            try:
                self.follower.disable_torque()
            except (RuntimeError, ValueError, OSError):
                self.stop_event.set()
                raise
        try:
            if any(value != 0 for value in read_complete(self.follower, 'Torque_Enable').values()):
                raise ValueError('Freedrive torque-release readback failed.')
        except (RuntimeError, ValueError, OSError):
            self.stop_event.set()
            raise
        self.released = True

    def receive(self, data):
        if not isinstance(data, dict) or data.get('protocol') != PROTOCOL or data.get('operation_id') != self.operation_id:
            return
        if not isinstance(data.get('request_id'), str) or not 1 <= len(data['request_id']) <= 64:
            return
        reply = {k: data[k] for k in ('protocol', 'operation_id', 'request_id')}
        reply.update(kind=data.get('kind'), mode='remoteoperate', session_id=self.mqtt.client_id)
        try:
            with self.lock:
                if not finite(data.get('timestamp')) or not -.25 <= time.time()-data['timestamp'] <= 1.5:
                    raise ValueError('Stale workflow request.')
                obs = self.observation(require_power=data.get('kind') == 'segment')
                if data.get('kind') == 'confirm':
                    if data.get('motion_profile') == COORDINATED:
                        if self.coordinated is None:
                            self.coordinated = Coordinated(self)
                        reply.update(self.coordinated.confirm(data, obs))
                    elif self.coordinated is not None:
                        raise ValueError('Cannot change motion profile inside an operation.')
                    else:
                        self.confirm(data, obs)
                        reply['status'] = 'ready'
                        reply['capabilities'] = ['persistent_freedrive', COORDINATED]
                elif data.get('kind') == 'finish':
                    if data.get('session_id') != self.mqtt.client_id or self.coordinated is None:
                        raise ValueError('Coordinated operation/session not active.')
                    reply.update(self.coordinated.finish(obs))
                elif data.get('kind') == 'release':
                    if self.coordinated is not None:
                        raise ValueError('Coordinated operations require measured finish.')
                    if data.get('session_id') != self.mqtt.client_id:
                        raise ValueError('Workflow session differs from active remoteoperate.')
                    self.release(obs)
                    reply.update(status='released', mode='freedrive')
                elif data.get('kind') == 'segment':
                    if data.get('session_id') != self.mqtt.client_id:
                        raise ValueError('Workflow session differs from active remoteoperate.')
                    if data['request_id'] in self.seen or len(self.seen) >= 256:
                        raise ValueError('Duplicate request or operation segment limit reached.')
                    if self.coordinated is not None:
                        reply.update(self.coordinated.segment(data, obs))
                        self.seen.add(data['request_id'])
                        reply['timestamp'] = time.time()
                        self.mqtt.publish(self.response_topic, reply, qos=0)
                        return
                    folded = data.get('supported_pose_radians', {})
                    if self.released:
                        raise ValueError('New operation confirmation required before motion.')
                    self.check_folded(obs, folded)
                    values, duration = trajectory(obs['shoulder_pan'], data['command'], data['limits_radians'], data['speed_degrees_s'])
                    mode = self.norm_modes['shoulder_pan']
                    calib = self.calibration.get('shoulder_pan')
                    schedule = schedule_chunks({'local_trajectory_hz': 30}, len(values))
                    actions = []
                    for value, meta in zip(values, schedule):
                        norm = self.from_rad(value, mode, calib)
                        valid, _ = self.validate(self.follower.motors['shoulder_pan'].id, norm)
                        converted = self.to_rad(norm, mode, calib)
                        if (not finite(norm) or not valid or not finite(converted)
                                or not math.isclose(converted, value, rel_tol=0, abs_tol=1e-9)):
                            raise ValueError('Driver position limit rejected the trajectory.')
                        actions.append({'shoulder_pan.pos': norm, '_delta_meta': {
                            **meta, 'workflow_v1': True, 'inter_chunk_delay_sec': 0}})
                    # Atomic whole-segment enqueue; no partly accepted trajectory.
                    with self.queue.mutex:
                        if self.stop_event.is_set() or self.queue.queue:
                            raise ValueError('Writer stopped or has pending commands.')
                        if self.queue.maxsize > 0 and len(actions) > self.queue.maxsize:
                            raise ValueError('Insufficient writer queue capacity.')
                        self.queue.queue.extend(actions)
                        self.queue.unfinished_tasks += len(actions)
                        self.queue.not_empty.notify()
                    self.seen.add(data['request_id'])
                    self.last_target, self.folded = values[-1], dict(folded)
                    reply.update(status='accepted', target_radians=values[-1],
                                 start_radians=values[0], duration_seconds=duration)
                else:
                    raise ValueError('Unsupported workflow request.')
        except (ValueError, KeyError, TypeError, RuntimeError, OSError) as exc:
            reply.update(status='rejected', detail=str(exc))
        reply['timestamp'] = time.time()
        self.mqtt.publish(self.response_topic, reply, qos=0)
