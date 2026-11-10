"""Request-correlated client for the prepared Cyberwave workflow extension."""
import queue
import time
import uuid

import robot
from workflow_protocol import PROTOCOL, COORDINATED, topics


class WorkflowRejected(RuntimeError):
    driver_rejected = True


class WorkflowClient:
    def __init__(self, channel, cancelled):
        self.channel, self.cancelled = channel, cancelled
        self.operation_id = uuid.uuid4().hex
        self.request_topic, self.response_topic = topics(channel.client.mqtt.topic_prefix, channel.twin)
        self.responses = queue.Queue(maxsize=64)
        channel.client.mqtt.subscribe(self.response_topic, self.receive)

    def receive(self, data):
        if isinstance(data, dict) and data.get('protocol') == PROTOCOL and data.get('operation_id') == self.operation_id:
            try:
                self.responses.put_nowait(dict(data))
            except queue.Full:
                pass

    def close(self):
        self.channel.client.mqtt.unsubscribe(self.response_topic)

    def exchange(self, kind, *, timeout, retry=False, on_publish=None, **fields):
        request = dict(protocol=PROTOCOL, kind=kind, operation_id=self.operation_id,
                       request_id=uuid.uuid4().hex, **fields)
        deadline, next_send = time.monotonic()+timeout, 0
        sent = None
        while time.monotonic() < deadline:
            if self.cancelled.is_set():
                raise RuntimeError('Command cancelled; no later segment sent.')
            if not self.channel.client.mqtt.connected:
                raise RuntimeError('Cyberwave connection lost before workflow acknowledgement.')
            if kind in {'segment','finish'}:
                if (not self.channel.client.mqtt.is_subscribed(self.response_topic)
                        or not self.channel.client.mqtt.is_subscribed(self.channel.topic)):
                    raise RuntimeError('Workflow or feedback subscription lost before trajectory acceptance.')
                state = self.channel.snapshot()
                if state is None or state['session_id'] != fields.get('session_id'):
                    raise RuntimeError('Fresh feedback/session lost while awaiting trajectory acceptance.')
            if time.monotonic() >= next_send:
                request['timestamp'] = time.time()
                if not self.channel.client.mqtt.publish(self.request_topic, dict(request), qos=0):
                    raise RuntimeError('Workflow publish failed.')
                if sent is None:
                    sent = request['timestamp']
                    if on_publish:
                        on_publish()
                next_send = time.monotonic()+.25 if retry else float('inf')
            try:
                response = self.responses.get_nowait()
            except queue.Empty:
                time.sleep(.02)
                continue
            stamp = response.get('timestamp')
            if (response.get('request_id') != request['request_id'] or response.get('kind') != kind
                    or not robot.finite(stamp)
                    or stamp < sent or not -.25 <= time.time()-stamp <= robot.MAX_AGE):
                continue
            if response.get('status') == 'rejected':
                raise WorkflowRejected('Driver rejected workflow: '+str(response.get('detail', 'unspecified')))
            expected = {'confirm':{'ready'},'segment':{'accepted'},'release':{'released'},'finish':{'held','released'}}[kind]
            if response.get('status') not in expected:
                continue
            expected_mode = 'freedrive' if response['status']=='released' else 'remoteoperate'
            if response.get('mode') != expected_mode:
                continue
            if kind == 'confirm' and 'persistent_freedrive' not in response.get('capabilities', []):
                raise RuntimeError('Driver does not support reusable freedrive sessions; no movement sent.')
            if (kind=='confirm' and fields.get('motion_profile')==COORDINATED
                    and COORDINATED not in response.get('capabilities', [])):
                raise RuntimeError('Driver lacks coordinated-joint support; no movement sent.')
            session = response.get('session_id')
            if not isinstance(session, str) or not session or ('session_id' in fields and session != fields['session_id']):
                continue
            return response
        raise RuntimeError('Workflow acknowledgement unavailable; compatible driver patch required. No retry of a motion target.')
