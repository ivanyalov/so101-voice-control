"""Isolated six-joint registers, virtual writer ticks and MQTT; never hardware."""
import contextlib
import io
import math
import os
from pathlib import Path
import queue
import sys
import threading
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'build'))
import robot
import voice_control as vc
import poses
from cyberwave_workflow_driver import Workflow
from workflow_fakes import add_torque_bus
from workflow_protocol import JOINTS


class Rig:
    def __init__(self, *, lost_joint=None, drift_joint=None, collision=False,
                 no_capability=False, no_feedback=False, cancel_first=False,
                 change_session=False, fail_second=False, lose_subscription=False,
                 reject_position=False, missing_collision_model=False):
        self.now = 0.
        self.norm = {n:0. for n in JOINTS}
        self.norm['gripper'] = 5.
        self.session = 'driver-session'
        self.cancelled = threading.Event()
        self.sent, self.responses, self.writes, self.subscribers = [], [], [], {}
        self.driver = None
        self.starts = 0
        self.options = locals().copy()
        self.client = Mock()
        self.client.mqtt.connected = True
        self.client.mqtt.topic_prefix = ''
        self.client.mqtt.is_subscribed.side_effect = lambda _:not lose_subscription
        self.channel = robot.Channel(self.client,'test-twin')
        self.client.mqtt.subscribe.side_effect = self.subscribe
        self.client.mqtt.publish.side_effect = self.publish
        self.follower = SimpleNamespace(connected=True,torque_enabled=True,
            _pin_model=None if missing_collision_model else object(),
            _pin_data=object(),_pin_collision_model=object(),_pin_collision_data=object(),_pin_floor_geom_id=0,
            motors={n:SimpleNamespace(id=i) for i,n in enumerate(JOINTS,1)},
            get_observation=lambda:{n+'.pos':v for n,v in self.norm.items()},
            validate_action_kinematics=Mock(return_value=(not collision,'')))
        self.bus = add_torque_bus(self.follower)
        self.modes = {n:SimpleNamespace(value='range_0_100' if n=='gripper' else 'range_m100_100') for n in JOINTS}
        self.mqtt = Mock(topic_prefix='',client_id=self.session,connected=True)
        self.mqtt.publish.side_effect = self.reply
        self.mqtt.subscribe.side_effect = self.subscribe
        self.folded = {n:math.degrees(self.to_rad(self.norm[n],self.modes[n],self.follower.calibration[n])) for n in JOINTS}
        self.sync_registers()

    @staticmethod
    def to_rad(value,mode,calibration):
        return value/100*(calibration.range_max-calibration.range_min)*(1 if mode.value=='range_0_100' else .5)*2*math.pi/4095

    @staticmethod
    def from_rad(value,mode,calibration):
        return value*100/((calibration.range_max-calibration.range_min)*(1 if mode.value=='range_0_100' else .5)*2*math.pi/4095)

    def sync_registers(self):
        self.bus.regs['Present_Position']={n:round(c.range_min+(self.norm[n] if n=='gripper' else (self.norm[n]+100)/2)/100*(c.range_max-c.range_min))
            for n,c in self.follower.calibration.items()}

    def subscribe(self,topic,callback,**kwargs):
        self.subscribers[topic]=callback
        self.feedback()

    def feedback(self):
        if self.options['no_feedback']:
            return
        self.channel.receive(dict(source_type='edge_follower',timestamp=1000+self.now,
            session_id=self.session,positions={key:self.to_rad(self.norm[n],self.modes[n],self.follower.calibration[n]) for n,key in JOINTS.items()}))

    def reply(self,topic,payload,**kwargs):
        self.responses.append(dict(payload))
        if self.options['no_capability'] and payload['kind']=='confirm':
            payload={**payload,'capabilities':['persistent_freedrive']}
        self.subscribers[topic](payload)
        return True

    def publish(self,topic,payload,**kwargs):
        self.sent.append((topic,dict(payload)))
        if payload.get('command')=='remoteoperate':
            if self.driver is None:
                self.starts+=1
                self.driver=Workflow(self.mqtt,'test-twin',payload['workflow_request'],self.follower,
                    threading.Event(),queue.Queue(maxsize=1000),self.modes,self.follower.calibration,
                    self.to_rad,self.from_rad,lambda *_:(not self.options['reject_position'],''))
                self.driver.start()
            else:
                self.driver.reserve(payload['workflow_request'])
            self.now+=.001
            self.feedback()
        elif payload.get('kind') in {'confirm','segment','finish','release'}:
            if (payload['kind']=='segment' and self.options['fail_second'] and self.channel.dispatched==1):
                return False
            self.driver.receive(payload)
            if payload['kind']=='segment':
                if self.options['cancel_first']:
                    self.cancelled.set()
                if self.options['change_session']:
                    self.session='unexpected'
        return True

    def sleep(self,seconds):
        self.now+=seconds
        if self.driver is not None:
            while self.driver.queue.queue:
                item=self.driver.queue.queue[0]
                clock=item['_delta_meta']['local_clock']
                if clock['start'] is None:
                    clock['start']=self.now
                if self.now<clock['start']+item['_delta_meta']['local_offset']:
                    break
                item=self.driver.queue.get_nowait()
                for name in JOINTS:
                    if name!=self.options['lost_joint']:
                        self.norm[name]=item[name+'.pos']
                self.writes.append((self.now,dict(item)))
                self.driver.queue.task_done()
            if self.options['drift_joint'] and self.channel.dispatched:
                self.norm[self.options['drift_joint']]+=1
            self.sync_registers()
        self.feedback()

    @contextlib.contextmanager
    def active(self):
        with patch.dict(os.environ,CYBERWAVE_TWIN_UUID='test-twin'), \
             patch('time.time',side_effect=lambda:1000+self.now), \
             patch('time.monotonic',side_effect=lambda:self.now), \
             patch('time.sleep',side_effect=self.sleep), \
             patch.object(poses,'load_pose',return_value=self.folded), \
             contextlib.redirect_stdout(io.StringIO()):
            yield self

    def execute(self,intent,profile=None):
        self.channel.dispatched=0
        self.client.mqtt.connected=True
        self.cancelled.clear()
        return vc.execute_intent(intent,profile=profile or {},channel_factory=lambda:self.channel,cancelled=self.cancelled)
