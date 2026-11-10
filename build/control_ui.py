"""Loopback-only SO101 command window over the existing terminal backend."""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import secrets
import shlex
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]
JOINTS = ('shoulder_pan', 'shoulder_lift', 'elbow_flex', 'wrist_flex', 'wrist_roll', 'gripper')


def taught_labels(text):
    import base_console
    try:
        return base_console.parse(text)
    except ValueError:
        return None


def run_taught(labels, preview):
    import base_console
    import robot
    if preview:
        profile = json.loads((ROOT / '.cache/robot/directions.json').read_text())
        result = base_console.prepare_sequence(labels, profile, os.getenv('CYBERWAVE_TWIN_UUID'))
        print(json.dumps(result), flush=True)
        return
    channel = robot.configured_channel()
    try:
        channel.client.twins.get(channel.twin)
        channel.connect()
        print(json.dumps({'status': 'completed', 'results': base_console.run_sequence(channel, labels)}), flush=True)
    finally:
        channel.client.disconnect()


def command_args(text):
    """An explicit allowlist, never a shell or arbitrary terminal command."""
    parts = shlex.split(text)
    if not parts:
        raise ValueError('Enter a command.')
    if parts[0] in {'status', 'read', 'start', 'stop', 'free'}:
        if len(parts) != 1:
            raise ValueError('This command takes no arguments.')
        return parts
    if parts[0] in {'jog', 'move'}:
        if len(parts) != 3 or parts[1] not in JOINTS:
            raise ValueError('Use jog/move JOINT DEGREES, or choose a joint on the left.')
        value = float(parts[2])
        if not math.isfinite(value):
            raise ValueError('Degrees must be finite.')
        return [parts[0], parts[1], str(value)]
    if parts[0] in {'pose', 'save-pose', 'terminal', 'voice', 'map-direction'}:
        raise ValueError('Pose replay and setup changes are unavailable in this interface. Smooth replay validation is pending.')
    return None


def interpret(text, preview):
    import voice_control
    result = voice_control.process_text(text, preview=preview, timeout=30)
    print(json.dumps(result, indent=2), flush=True)


def worker(args):
    from dotenv import load_dotenv
    load_dotenv(ROOT / '.env', override=False)
    if args.capture:
        import readiness
        from voice_toggle import Recorder
        recorder = Recorder(max_seconds=30)
        try:
            recorder.start()
            print('RECORDING. Click Finish recording. Maximum 30 seconds.', flush=True)
            done = threading.Event()
            threading.Thread(target=lambda: (sys.stdin.readline(), done.set()), daemon=True).start()
            while not done.wait(.05) and not recorder.full.is_set():
                pass
            audio = recorder.stop()
            print('Microphone off. Transcribing locally…', flush=True)
            text = readiness.transcribe(audio)['text']
            print('TRANSCRIPT: ' + text, flush=True)
        finally:
            recorder.cancel()
    else:
        text = args.text
    labels = taught_labels(text)
    if labels:
        run_taught(labels, args.preview)
        return
    native = command_args(text)
    if native:
        if args.preview and native[0] not in {'read', 'status', 'stop'}:
            print(json.dumps({'status': 'preview', 'command': native, 'motion_sent': False}), flush=True)
        else:
            subprocess.run([sys.executable, str(ROOT / 'build/robot.py'), '--timeout', '30', *native], check=False)
    else:
        interpret(text, args.preview)


class Console:
    def __init__(self, demo=False):
        self.demo = demo
        self.lock = threading.RLock()
        self.process = None
        self.busy = False
        self.recording = False
        self.events = []
        self.serial = 0
        self.add('system', 'Ready. No robot command has been sent.' + (' DEMO: hardware and microphone disabled.' if demo else ''))

    def add(self, kind, text):
        with self.lock:
            self.serial += 1
            self.events.append({'id': self.serial, 'kind': kind, 'text': text})
            self.events = self.events[-500:]

    def state(self):
        with self.lock:
            return {'busy': self.busy, 'recording': self.recording, 'events': list(self.events), 'demo': self.demo}

    def launch(self, argv):
        self.process = subprocess.Popen(argv, cwd=ROOT, stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                        text=True, bufsize=1, start_new_session=True)
        return self.process

    def submit(self, text='', preview=True, capture=False):
        if not isinstance(text, str) or len(text) > 2000 or type(preview) is not bool:
            raise ValueError('Invalid command.')
        native = None if capture else command_args(text)
        with self.lock:
            if self.busy:
                raise ValueError('Another command is active. Wait, or use Stop.')
            self.add('you', ('Record voice' if capture else text) + (' [preview]' if preview else ' [execute]'))
            self.busy = True
            if self.demo:
                self.add('result', 'DEMO only: ' + ('Microphone disabled.' if capture else 'Command received; no backend called.'))
                self.busy = False
                return
            if native and (not preview or native[0] in {'status', 'read', 'stop'}):
                argv = [sys.executable, str(ROOT / 'build/robot.py'), '--timeout', '30', *native]
            else:
                argv = [sys.executable, '-u', __file__, '--worker']
                argv += ['--capture'] if capture else ['--text', text]
                if preview:
                    argv.append('--preview')
            try:
                process = self.launch(argv)
            except Exception:
                self.busy = False
                raise
            threading.Thread(target=self.collect, args=(process,), daemon=True).start()

    def collect(self, process):
        try:
            for line in process.stdout:
                with self.lock:
                    if self.process is not process:
                        break
                    if line.startswith('RECORDING.'):
                        self.recording = True
                    if line.startswith('Microphone off.'):
                        self.recording = False
                    self.add('result', line.rstrip())
            code = process.wait()
            with self.lock:
                if self.process is process:
                    self.add('system', f'Command finished (exit {code}).')
        finally:
            with self.lock:
                if self.process is process:
                    self.process = None
                    self.busy = self.recording = False

    def finish(self):
        with self.lock:
            if not self.recording or not self.process:
                raise ValueError('Microphone is not recording.')
            self.process.stdin.write('\n')
            self.process.stdin.flush()
            self.recording = False

    def cancel_local(self):
        """Kill descendants too: no delayed planner or sequence can dispatch later."""
        with self.lock:
            process, self.process = self.process, None
            self.recording = False
            if process:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=5)

    def stop(self):
        with self.lock:
            self.cancel_local()
            self.busy = True
            self.add('you', 'stop [execute]')
            if self.demo:
                self.add('result', 'DEMO: local command cancelled; no driver stop sent.')
                self.busy = False
                return
            try:
                process = self.launch([sys.executable, str(ROOT / 'build/robot.py'), '--timeout', '8', 'stop'])
            except Exception:
                self.busy = False
                raise
            threading.Thread(target=self.collect, args=(process,), daemon=True).start()


def serve(port, demo):
    console = Console(demo)
    token = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, code, value, content_type='application/json'):
            data = value if isinstance(value, bytes) else json.dumps(value).encode()
            self.send_response(code)
            self.send_header('Content-Type', content_type)
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'")
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def authorized(self):
            expected = f'127.0.0.1:{self.server.server_port}'
            return (self.headers.get('Host') == expected and
                    self.headers.get('Origin', f'http://{expected}') == f'http://{expected}' and
                    secrets.compare_digest(self.headers.get('X-Control-Token', ''), token))

        def do_GET(self):
            if self.path == '/api/state':
                self.reply(200, console.state()) if self.authorized() else self.reply(403, {'error': 'Access denied.'})
                return
            files = {'/': ('index.html', 'text/html; charset=utf-8'), '/app.js': ('app.js', 'text/javascript'), '/style.css': ('style.css', 'text/css')}
            if self.path not in files:
                self.reply(404, {'error': 'Not found.'})
                return
            name, mime = files[self.path]
            self.reply(200, (ROOT / 'build/control_ui' / name).read_bytes(), mime)

        def do_POST(self):
            if not self.authorized():
                self.reply(403, {'error': 'Access denied.'})
                return
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 8192:
                    raise ValueError('Invalid request size.')
                body = json.loads(self.rfile.read(length))
                if self.path == '/api/command':
                    # Typed stop must interrupt immediately, just like the Stop button.
                    if body.get('text', '').strip().lower() == 'stop':
                        console.stop()
                    else:
                        console.submit(body.get('text', ''), body.get('preview', True))
                elif self.path == '/api/record':
                    console.submit(preview=body.get('preview', True), capture=True)
                elif self.path == '/api/finish':
                    console.finish()
                elif self.path == '/api/stop':
                    console.stop()
                else:
                    raise ValueError('Unknown action.')
                self.reply(200, {'ok': True})
            except (ValueError, TypeError, AttributeError, OSError) as exc:
                self.reply(400, {'error': str(exc)})

    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    print(f'SO101 control: http://127.0.0.1:{server.server_port}/#{token}', flush=True)
    print('Opening the page sends no robot command. Ctrl+C closes the UI; use Stop before closing if a target is active.', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        console.cancel_local()
        server.server_close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--demo', action='store_true', help='Disable robot, planner and microphone access')
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--capture', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--text', default='', help=argparse.SUPPRESS)
    parser.add_argument('--preview', action='store_true', help=argparse.SUPPRESS)
    options = parser.parse_args()
    try:
        worker(options) if options.worker else serve(options.port, options.demo)
    except Exception as exc:
        print(f'Failed: {exc}' if isinstance(exc, (ValueError, RuntimeError, OSError)) else f'Failed: {type(exc).__name__}', flush=True)
        raise SystemExit(1)
