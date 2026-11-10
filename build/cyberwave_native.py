"""Native Cyberwave SO101 host. Preparation/checks never open a motor port.

Uses the reviewed official driver and existing workflow patches, not another
serial controller. Native startup stays idle until an explicit fresh command.
"""
from __future__ import annotations

import argparse
import asyncio
import functools
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / '.cache/cyberwave-native'
SOURCE = STATE / 'source'
LABEL = 'com.ivanyalov.so101.native'
LEGACY = ('com.cyberwave.edge.core', 'com.cyberwave.serial-bridge')
JOINTS = ('shoulder_pan', 'shoulder_lift', 'elbow_flex', 'wrist_flex', 'wrist_roll', 'gripper')


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as stream:
        json.dump(data, stream, indent=2, allow_nan=False)
        tmp = Path(stream.name)
    os.replace(tmp, path)


def native_port(port):
    if not isinstance(port, str) or not re.fullmatch(r'/dev/cu\.[A-Za-z0-9_.-]+', port):
        raise ValueError('Native Mac port must be an explicit /dev/cu.* device; no PTY, TCP or URL.')
    return port


def prepare(port=None):
    """Copy reviewed sources/configuration without changing the existing stack."""
    if SOURCE.exists():
        raise RuntimeError('Native bundle already exists; check it instead of overwriting it.')
    config = Path.home() / '.cyberwave'
    setup = json.loads((config / 'so101_lib/setup.json').read_text())
    if port is None:
        bridges = json.loads((config / 'serial_bridges.json').read_text()).get('bridges', [])
        if len(bridges) != 1:
            raise ValueError('Pass --port: the existing bridge does not identify one unique device.')
        port = bridges[0]['device']
    port = native_port(port)
    STATE.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=STATE, prefix='prepare-') as tmp:
        tmp = Path(tmp)
        source = tmp / 'source'
        source.mkdir()
        original = ROOT / '.cache/cyberwave-edge-so101'
        for name in ('motors', 'so101', 'utils', 'scripts'):
            shutil.copytree(original / name, source / name,
                            ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        for name in ('main.py', 'LICENSE', 'pyproject.toml'):
            shutil.copyfile(original / name, source / name)
        originals = {
            'so101/follower.py': '.cache/cyberwave-safe-image/follower.py.original',
            'scripts/cw_remoteoperate.py': '.cache/cyberwave-safe-image/cw_remoteoperate.py.original',
            'utils/cw_remoteoperate_helpers.py': '.cache/robot/cw_remoteoperate_helpers.before-smooth.py',
        }
        for dest, src in originals.items():
            shutil.copyfile(ROOT / src, source / dest)
        for name in ('cyberwave-preserve-pose.patch', 'cyberwave-local-trajectory.patch'):
            subprocess.run(['patch', '--batch', '--fuzz=0', '-p1', '-i', str(ROOT/'build'/name)],
                           cwd=source, check=True, capture_output=True)
        from cyberwave_image import verify_workflow_inputs
        verify_workflow_inputs(source)
        subprocess.run(['patch', '--batch', '--fuzz=0', '-p1', '-i', str(ROOT/'build/cyberwave-workflow-v1.patch')],
                       cwd=source, check=True, capture_output=True)
        for src, dest in {
            'cyberwave_hold.py': 'so101/session_hold.py',
            'cyberwave_trajectory.py': 'utils/cw_local_trajectory.py',
            'cyberwave_workflow_driver.py': 'utils/cw_workflow.py',
            'coordinated_driver.py': 'utils/cw_coordinated_driver.py',
            'workflow_protocol.py': 'utils/cw_workflow_protocol.py',
        }.items():
            shutil.copyfile(ROOT/'build'/src, source/dest)
        lib = tmp / 'config/so101_lib'
        shutil.copytree(config/'so101_lib/calibrations', lib/'calibrations',
                        ignore=shutil.ignore_patterns('_old'))
        shutil.copytree(config/'so101_lib/urdf', lib/'urdf',
                        ignore=shutil.ignore_patterns('__MACOSX', '._*'))
        calibration = json.loads((lib/'calibrations/follower1.json').read_text())
        if set(calibration) != set(JOINTS):
            raise ValueError('Existing complete six-joint calibration required.')
        setup.update(follower_port=port, follower_id='follower1', leader_port=None,
                     cameras=[], camera_only=False)
        write_json(lib/'setup.json', setup)
        env = json.loads((config/'environment.json').read_text())
        manifest = dict(port=port, twin=setup['twin_uuid'], workspace=env['workspace_uuid'],
                        protocol='so101.workflow.v1', source_origin='reviewed local Cyberwave 0.3.0 snapshot',
                        sha256={str(p.relative_to(tmp)):digest(p) for p in tmp.rglob('*') if p.is_file()})
        for p in source.rglob('*.py'):
            compile(p.read_text(), str(p), 'exec')
        shutil.move(str(source), SOURCE)
        shutil.move(str(tmp/'config'), STATE/'config')
        write_json(STATE/'manifest.json', manifest)
    return dict(status='prepared', port=port, port_present=Path(port).exists(), motor_io=False)


def verify():
    manifest = json.loads((STATE/'manifest.json').read_text())
    native_port(manifest['port'])
    for name, expected in manifest['sha256'].items():
        if digest(STATE/name) != expected:
            raise RuntimeError('Native bundle/configuration changed: '+name)
    expected = {n for n in manifest['sha256'] if n.startswith('source/') and n.endswith('.py')}
    actual = {str(p.relative_to(STATE)) for p in SOURCE.rglob('*.py')}
    if actual != expected:
        raise RuntimeError('Native source inventory differs from reviewed bundle.')
    return manifest


def configure_environment(manifest):
    from dotenv import dotenv_values
    values = dotenv_values(ROOT/'.env')
    token = values.get('CYBERWAVE_API_KEY') or os.getenv('CYBERWAVE_API_KEY')
    if not token:
        token = json.loads((Path.home()/'.cyberwave/credentials.json').read_text())['token']
    os.environ.update(CYBERWAVE_API_KEY=token, CYBERWAVE_TWIN_UUID=manifest['twin'],
                      CYBERWAVE_WORKSPACE_ID=manifest['workspace'],
                      CYBERWAVE_EDGE_CONFIG_DIR=str(STATE/'config'),
                      CYBERWAVE_PRESERVE_POSE='1', CYBERWAVE_SO101_IDLE_CAMERAS='false',
                      CYBERWAVE_VIDEO_SYNC_REQUIRED='false')
    sys.path.insert(0, str(SOURCE))


class Feedback:
    """Driver-local measurements, independent of cloud publication latency."""
    def __init__(self):
        self.lock = threading.Lock()
        self.data = dict(attempts=0, complete_reads=0, failed_reads=0,
                         last_attempt=None, last_measured=None, positions_radians=None)

    def record(self, positions):
        valid = (isinstance(positions, dict) and set(positions)=={f'_{i}' for i in range(1,7)}
                 and all(isinstance(v,(int,float)) and not isinstance(v,bool) and math.isfinite(v)
                         for v in positions.values()))
        with self.lock:
            self.data['attempts'] += 1
            self.data['last_attempt'] = time.time()
            self.data['complete_reads' if valid else 'failed_reads'] += 1
            self.data['positions_radians'] = dict(positions) if valid else None
            if valid:
                self.data['last_measured'] = self.data['last_attempt']
        return valid

    def snapshot(self):
        with self.lock:
            result = dict(self.data)
        age = None if result['last_measured'] is None else time.time()-result['last_measured']
        result.update(measurement_age_seconds=age,
                      feedback='fresh' if result['positions_radians'] is not None and age is not None and 0<=age<=1.5
                      else 'unavailable')
        if result['feedback']!='fresh':
            result['positions_radians'] = None
        return result


def install_guards(driver, feedback):
    """Keep the official worker/lifecycle; narrow its native command entry."""
    original = driver.handle_command

    def command(client, twin, name, payload):
        cmd = name.removeprefix('so101-') if isinstance(name,str) else ''
        stamp = payload.get('timestamp')
        fresh = (isinstance(stamp,(int,float)) and not isinstance(stamp,bool)
                 and math.isfinite(stamp) and -.25<=time.time()-stamp<=10)
        if cmd=='native-status':
            request = payload.get('request_id')
            if fresh and isinstance(request,str) and re.fullmatch('[0-9a-f]{32}',request):
                client.mqtt.publish(f'{client.mqtt.topic_prefix}cyberwave/twin/{twin}/native/status/{request}',
                    dict(request_id=request,timestamp=time.time(),transport='native_usb',
                         operation_active=bool(driver._current_thread and driver._current_thread.is_alive()),
                         **feedback.snapshot()),qos=0)
            return
        allowed = cmd=='stop' or (cmd=='freedrive' and fresh)
        if cmd=='remoteoperate':
            allowed = (isinstance(payload.get('workflow_request'),str)
                       and re.fullmatch('[0-9a-f]{32}',payload['workflow_request']) is not None
                       and fresh)
        if not allowed:
            driver.logger.warning('Native host rejected non-operator/legacy command: %s', name)
            return
        original(client,twin,name,payload)

    driver.handle_command = command
    # These stock boot paths can probe hardware, rewrite port configuration,
    # download models, calibrate, or resume an assigned controller automatically.
    driver._ensure_setup = lambda *_: None
    driver._ensure_so101_urdf = lambda: None
    driver._refresh_so101_ports_in_setup = lambda: None
    driver._check_startup_calibration = lambda *_: driver.logger.info(
        'Native startup idle: no motor port opened, calibration or controller resumed.')
    driver._evaluate_and_drive = lambda *_: driver.logger.info(
        'Native controller auto-resume withheld; a fresh operator workflow request is required.')
    def connection_lost(client, twin, on_trip, stop_event=None):
        # Keep the official local stop/hold path. Do not detach/re-attach a
        # dashboard controller or infer torque release from a stop request.
        driver.logger.error('MQTT lost during operation: stopping/holding; no automatic resume.')
        driver._stop_current_operation(client,twin)
        return True  # Native host stays idle; MQTT reconnects independently.
    driver._handle_mqtt_connection_lost = connection_lost
    create_client = driver.Cyberwave
    def client_factory(*args, **kwargs):
        driver._native_client = create_client(*args, **kwargs)
        return driver._native_client
    driver.Cyberwave = client_factory
    last_health_count = [0]
    def joint_health():
        if driver._edge_health is None:
            return {}
        state = feedback.snapshot()
        if state['feedback']=='fresh' and state['complete_reads']>last_health_count[0]:
            driver._edge_health.update_frame_count()
            last_health_count[0]=state['complete_reads']
        return {driver.SO101_JOINT_STREAM_ID:dict(kind='imu',
            source=f'cyberwave/joint/{driver._edge_health.edge_id}/update',
            rate_hz=driver.SO101_JOINT_STREAM_RATE_HZ)}
    # A present USB device or a running thread is not a successful servo read.
    driver._so101_joint_stream_config_provider = joint_health

    from utils.cw_freedrive import FreedriveSession
    from so101.follower import SO101Follower
    from utils.utils import normalized_to_radians
    read_free = FreedriveSession.read_joint_positions

    @functools.wraps(read_free)
    def measured_free(session):
        positions = read_free(session)
        if feedback.record(positions):
            session._native_empty_reads = 0
            return positions
        session._native_empty_reads = getattr(session,'_native_empty_reads',0)+1
        # The stock loop reconnects on None, but retries {} forever. Bound that
        # recovery hole and never publish a partial six-joint measurement.
        return None if positions is None or session._native_empty_reads>=5 else {}
    FreedriveSession.read_joint_positions = measured_free
    read = SO101Follower.get_observation

    @functools.wraps(read)
    def measured_follower(follower):
        try:
            obs = read(follower)
        except Exception:
            feedback.record(None)
            raise
        if not isinstance(obs,dict) or not all(n+'.pos' in obs for n in JOINTS):
            feedback.record(None)
            return {}
        try:
            positions = {f'_{follower.motors[n].id}':normalized_to_radians(
                obs[n+'.pos'],follower.motors[n].norm_mode,follower.calibration[n]) for n in JOINTS}
        except (TypeError,KeyError,ValueError):
            feedback.record(None)
            return {}
        return obs if feedback.record(positions) else {}
    SO101Follower.get_observation = measured_follower


def check():
    manifest = verify()
    configure_environment(manifest)
    from so101.follower import SO101Follower
    from utils.config import FollowerConfig, get_so101_urdf_path
    follower = SO101Follower(FollowerConfig(port=manifest['port']))
    follower.init_pin(get_so101_urdf_path(), floor_z=0.)
    if follower._pin_model is None or follower._pin_collision_model is None or follower._pin_floor_geom_id is None:
        raise RuntimeError('Native collision/floor geometry failed to load.')
    for name in ('scripts.cw_remoteoperate','utils.cw_workflow','utils.cw_coordinated_driver'):
        __import__(name)
    import importlib.metadata as metadata
    if metadata.version('cyberwave')!='0.7.4':
        raise RuntimeError('Unreviewed Cyberwave SDK version.')
    return dict(status='offline_checks_passed',port=manifest['port'],port_present=Path(manifest['port']).exists(),
                sdk=metadata.version('cyberwave'), collision_geometry=True, floor_geometry=True, motor_io=False)


def run():
    manifest = verify()
    configure_environment(manifest)
    spec = importlib.util.spec_from_file_location('native_cyberwave_driver',SOURCE/'main.py')
    driver = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(driver)
    feedback, stop = Feedback(), threading.Event()
    install_guards(driver,feedback)
    def monitor():
        while not stop.is_set():
            client = getattr(driver,'_native_client',None)
            write_json(STATE/'status.json',dict(pid=os.getpid(),timestamp=time.time(),transport='native_usb',
                port=manifest['port'],port_present=Path(manifest['port']).exists(),
                mqtt_connected=bool(client and client.mqtt.connected),
                operation_active=bool(driver._current_thread and driver._current_thread.is_alive()),
                **feedback.snapshot()))
            stop.wait(.25)
    thread = threading.Thread(target=monitor,daemon=True)
    thread.start()
    try:
        asyncio.run(driver.main())
    finally:
        stop.set()
        thread.join(timeout=2)
        write_json(STATE/'status.json',dict(pid=os.getpid(),timestamp=time.time(),status='stopped',feedback='unavailable'))


def call(args, *, check=True):
    return subprocess.run(args, check=check, capture_output=True, text=True)


def domain():
    return f'gui/{os.getuid()}'


def require_disconnected(manifest):
    from serial.tools import list_ports
    if Path(manifest['port']).exists() or any(p.vid==0x1a86 and p.pid==0x55d3 for p in list_ports.comports()):
        raise RuntimeError('Disconnect the arm before changing driver ownership; no service changed.')


def activate():
    manifest = verify()
    if (STATE/'activation.json').exists():
        previous = json.loads((STATE/'activation.json').read_text())
        if previous['stage']=='native_started':
            if call(['launchctl','print',domain()+'/'+LABEL],check=False).returncode:
                call(['launchctl','bootstrap',domain(),str(STATE/'service.plist')])
            else:
                call(['launchctl','kickstart',domain()+'/'+LABEL])
            return dict(status='native_started',motor_io=False)
        if previous['stage']!='rolled_back':
            raise RuntimeError('Incomplete native activation; use rollback before trying again.')
    require_disconnected(manifest)
    disabled = call(['launchctl','print-disabled',domain()]).stdout
    legacy = {}
    for label in LEGACY:
        legacy[label] = dict(loaded=call(['launchctl','print',domain()+'/'+label],check=False).returncode==0,
            disabled=re.search(r'"'+re.escape(label)+r'"\s*=>\s*true',disabled) is not None)
    container = 'cyberwave-driver-'+manifest['twin'].split('-')[0]
    inspected = call(['docker','inspect','--format','{{json .HostConfig.RestartPolicy}} {{.State.Running}}',container])
    policy, running = inspected.stdout.strip().rsplit(' ',1)
    policy = json.loads(policy)
    write_json(STATE/'activation.json',dict(legacy=legacy,container=container,restart_policy=policy,
                                           container_running=running=='true',stage='switching'))
    # Disable relaunch before stopping the sole old driver and serial owner.
    for label, previous in legacy.items():
        call(['launchctl','disable',domain()+'/'+label])
        if previous['loaded']:
            call(['launchctl','bootout',domain()+'/'+label])
    call(['docker','update','--restart=no',container])
    if running=='true':
        call(['docker','stop','--time','10',container])
    plist = dict(Label=LABEL,ProgramArguments=[sys.executable,str(Path(__file__).resolve()),'run'],
                 WorkingDirectory=str(ROOT),RunAtLoad=True,KeepAlive=False,
                 StandardOutPath=str(STATE/'native.log'),StandardErrorPath=str(STATE/'native.log'))
    (STATE/'service.plist').write_bytes(plistlib.dumps(plist))
    call(['launchctl','enable',domain()+'/'+LABEL])
    call(['launchctl','bootstrap',domain(),str(STATE/'service.plist')])
    state = json.loads((STATE/'activation.json').read_text())
    state['stage']='native_started'
    write_json(STATE/'activation.json',state)
    return dict(status='native_started',serial_bridge='disabled',old_container='stopped',motor_io=False)


def rollback():
    require_disconnected(verify())
    state = json.loads((STATE/'activation.json').read_text())
    call(['launchctl','bootout',domain()+'/'+LABEL],check=False)
    policy = state['restart_policy']
    name = policy['Name'] or 'no'
    if name=='on-failure' and policy.get('MaximumRetryCount'):
        name += ':'+str(policy['MaximumRetryCount'])
    call(['docker','update','--restart='+name,state['container']])
    for label, previous in state['legacy'].items():
        call(['launchctl','disable' if previous['disabled'] else 'enable',domain()+'/'+label])
        if previous['loaded'] and call(['launchctl','print',domain()+'/'+label],check=False).returncode:
            call(['launchctl','bootstrap',domain(),str(Path.home()/'Library/LaunchAgents'/f'{label}.plist')])
    if state['container_running']:
        call(['docker','start',state['container']])
    state['stage']='rolled_back'
    write_json(STATE/'activation.json',state)
    return dict(status='rolled_back',motor_io=False)


def status():
    manifest = verify()
    state = json.loads((STATE/'status.json').read_text()) if (STATE/'status.json').exists() else {}
    heartbeat_age = time.time()-state['timestamp'] if state else None
    running = (state.get('status')!='stopped' and heartbeat_age is not None and 0<=heartbeat_age<=2)
    measured = state.get('last_measured')
    age = None if measured is None else time.time()-measured
    if not running or age is None or not 0<=age<=1.5:
        state.update(feedback='unavailable',positions_radians=None)
    state['measurement_age_seconds']=age
    return dict(**state, native_process_running=running, heartbeat_age_seconds=heartbeat_age,
                configured_port=manifest['port'],device_present=Path(manifest['port']).exists())


def probe(timeout=15):
    """Round trip to the actual native command worker; no motor access."""
    import robot
    channel = robot.configured_channel()
    request, received, result = uuid.uuid4().hex, threading.Event(), {}
    topic = f'{channel.client.mqtt.topic_prefix}cyberwave/twin/{channel.twin}/native/status/{request}'
    def response(data):
        if (isinstance(data,dict) and data.get('request_id')==request and data.get('transport')=='native_usb'
                and robot.finite(data.get('timestamp')) and -.25<=time.time()-data['timestamp']<=1.5):
            result.update(data)
            received.set()
    try:
        channel.client.mqtt.connect()
        channel.client.mqtt.subscribe(topic,response)
        deadline = time.monotonic()+timeout
        while not channel.client.mqtt.is_subscribed(topic):
            if time.monotonic()>=deadline:raise RuntimeError('Native probe subscription was not acknowledged.')
            time.sleep(.02)
        began = time.monotonic()
        command_topic=f'{channel.client.mqtt.topic_prefix}cyberwave/twin/{channel.twin}/command'
        if not channel.client.mqtt.publish(command_topic,dict(command='native-status',request_id=request,
                                                             timestamp=time.time()),qos=0):
            raise RuntimeError('Native diagnostic publish failed.')
        if not received.wait(max(0,deadline-time.monotonic())):
            raise RuntimeError('No reply from the native command worker.')
        evidence = dict(status='native_worker_replied',round_trip_seconds=round(time.monotonic()-began,3),
                        motor_io=False,driver=result)
        write_json(STATE/'last-probe.json',evidence)
        return evidence
    finally:
        channel.client.mqtt.disconnect()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=('prepare','check','run','start','status','probe','stop','rollback'))
    parser.add_argument('--port')
    args = parser.parse_args()
    if args.command=='prepare':result=prepare(args.port)
    elif args.command=='check':result=check()
    elif args.command=='run':run();return
    elif args.command=='start':check();result=activate()
    elif args.command=='rollback':result=rollback()
    elif args.command=='probe':result=probe()
    elif args.command=='stop':
        call(['launchctl','bootout',domain()+'/'+LABEL]);result={'status':'native_stopped'}
    else:result=status()
    print(json.dumps(result,indent=2,allow_nan=False))


if __name__=='__main__':
    try:
        main()
    except (OSError,ValueError,RuntimeError,subprocess.CalledProcessError) as exc:
        # Do not print subprocess output or credentials from SDK exceptions.
        print(f'Native Cyberwave setup failed: {type(exc).__name__}: {exc}',file=sys.stderr)
        sys.exit(1)
