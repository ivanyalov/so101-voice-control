"""Toggle local capture, transcribe with Whisper and interpret commands with Grok."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
import queue
import sys
import threading
import time
from zoneinfo import ZoneInfo

import readiness
import command_preview
from turn_log import TurnLog

HOTKEY_LABEL = "Control + Command + Shift + R"
RATE = 16000


class Shortcut:
    """Activate once per physical R press, independent of keyboard layout."""

    def __init__(self, keyboard, emit, *, mac: bool):
        self.keyboard = keyboard
        self.emit = emit
        self.mac = mac
        self.modifiers = set()
        self.down = False

    def is_trigger(self, key):
        # macOS virtual key 15 is the physical R key, independent of layout.
        if self.mac:
            return getattr(key, "vk", None) == 15
        return (getattr(key, "char", None) or "").lower() == "r"

    def press(self, key, injected=False):
        if injected:
            return
        if key in {self.keyboard.Key.ctrl_l, self.keyboard.Key.ctrl_r,
                   self.keyboard.Key.cmd_l, self.keyboard.Key.cmd_r,
                   self.keyboard.Key.shift_l, self.keyboard.Key.shift_r,
                   self.keyboard.Key.alt_l, self.keyboard.Key.alt_r}:
            self.modifiers.add(key)
        if self.is_trigger(key):
            ctrl = bool(self.modifiers & {self.keyboard.Key.ctrl_l, self.keyboard.Key.ctrl_r})
            cmd = bool(self.modifiers & {self.keyboard.Key.cmd_l, self.keyboard.Key.cmd_r})
            shift = bool(self.modifiers & {self.keyboard.Key.shift_l, self.keyboard.Key.shift_r})
            alt = bool(self.modifiers & {self.keyboard.Key.alt_l, self.keyboard.Key.alt_r})
            if not self.down and ctrl and cmd and shift and not alt:
                self.emit()
            self.down = True

    def release(self, key, injected=False):
        if injected:
            return
        self.modifiers.discard(key)
        if self.is_trigger(key):
            self.down = False


class Recorder:
    """Open the mic only during a recording; bound audio kept in memory."""

    def __init__(self, max_seconds: float = 30):
        if not 1 <= max_seconds <= 120:
            raise ValueError("Maximum recording length must be between 1 and 120 seconds")
        self.limit = int(max_seconds * RATE)
        self.stream = None
        self.chunks = []
        self.samples = 0
        self.full = threading.Event()
        self.audio_status = set()

    @property
    def recording(self) -> bool:
        return self.stream is not None

    def _capture(self, data, frames, timing, status):
        import sounddevice as sd

        if status:
            self.audio_status.add(str(status))
        remaining = self.limit - self.samples
        chunk = data[:remaining].copy()
        self.chunks.append(chunk)
        self.samples += len(chunk)
        if self.samples >= self.limit:
            self.full.set()
            raise sd.CallbackStop

    def start(self) -> None:
        import sounddevice as sd

        if self.recording:
            raise RuntimeError("Microphone is already recording")
        self.chunks = []
        self.samples = 0
        self.audio_status.clear()
        self.full.clear()
        stream = sd.InputStream(samplerate=RATE, channels=1, dtype="int16", callback=self._capture)
        try:
            stream.start()
        except BaseException:
            stream.close()
            raise
        self.stream = stream

    def _close(self) -> None:
        stream, self.stream = self.stream, None
        if stream is not None:
            try:
                stream.stop()
            finally:
                stream.close()

    def stop(self) -> Path:
        import numpy as np
        import soundfile as sf

        self._close()
        if self.samples < RATE // 4:
            self.chunks = []
            raise ValueError("Recording was too short. Try speaking for at least a quarter second.")
        output = readiness.ROOT / ".cache/audio" / f"command-{time.time_ns()}.wav"
        output.parent.mkdir(parents=True, exist_ok=True)
        try:
            sf.write(output, np.concatenate(self.chunks), RATE, subtype="PCM_16")
        finally:
            self.chunks = []
        if self.audio_status:
            print("Audio warning: " + "; ".join(sorted(self.audio_status)), flush=True)
        return output

    def cancel(self) -> None:
        try:
            self._close()
        finally:
            self.chunks = []


class Session:
    """All state changes run on the main loop; transcription runs in a worker."""

    def __init__(self, recorder: Recorder, controls: str = "Enter", processor=None, log=None, on_start=None):
        self.recorder = recorder
        self.results = queue.Queue()
        self.busy = False
        self.worker = None
        self.controls = controls
        self.processor = processor or command_preview.preview
        self.log = log
        self.turn = 0
        self.on_start = on_start

    def event(self, name: str, **data):
        if self.log is not None:
            self.log.write(name, turn=self.turn, **data)

    def ready(self):
        print(f"READY. Microphone off. {self.controls}: start recording.", flush=True)

    def toggle(self) -> None:
        if self.busy:
            print("Processing. Wait for READY before recording again.", flush=True)
            return
        try:
            if not self.recorder.recording:
                if self.on_start is not None:
                    self.on_start()
                self.recorder.start()
                self.turn += 1
                self.event("recording_started")
                print(f"RECORDING. {self.controls}: stop recording.", flush=True)
                return
            audio = self.recorder.stop()
            self.event("recording_stopped", audio_file=str(audio))
            self.busy = True
            print("Microphone OFF. Transcribing locally...", flush=True)
            self.worker = threading.Thread(target=self._transcribe, args=(audio,))
            self.worker.start()
        except Exception as exc:
            self.recorder.cancel()
            self.event("recording_failed", error=str(exc))
            print(f"Recording failed: {exc}", flush=True)
            self.ready()

    def _transcribe(self, audio: Path) -> None:
        result = None
        try:
            started = time.perf_counter()
            result = readiness.transcribe(audio)
            transcribed = time.perf_counter()
            result['transcription_elapsed_seconds'] = round(transcribed-started, 3)
            print("TRANSCRIPT: " + result["text"], flush=True)
            self.event("transcript", audio_file=str(audio), result=result)
            self.event("planner_started")
            plan = self.processor(result["text"])
            finished = time.perf_counter()
            result = {**result, **plan,
                      "transcription_elapsed_seconds": round(transcribed - started, 3),
                      "command_processing_elapsed_seconds": round(finished - transcribed, 3),
                      "processing_elapsed_seconds": round(finished - started, 3),
                      "audio_file": str(audio)}
            self.event("result", result=result)
            self.results.put((result, None))
        except Exception as exc:
            self.event("transcription_failed" if result is None else "planner_failed", error=str(exc))
            self.results.put((result, exc))

    def poll(self) -> None:
        if self.recorder.recording and self.recorder.full.is_set():
            self.event("recording_time_limit")
            print("Recording time limit reached.", flush=True)
            self.toggle()
        try:
            result, error = self.results.get_nowait()
        except queue.Empty:
            return
        self.busy = False
        if error is not None:
            if result is None:
                print(f"Transcription failed: {error}", flush=True)
            else:
                print(json.dumps({**result, "status": "planner_failed", "error": str(error), "executed_actions": 0}, indent=2), flush=True)
        else:
            print(json.dumps(result, indent=2), flush=True)
        self.ready()


def run(*, terminal: bool = False, max_seconds: float = 30, planner: str = "grok",
        paired: bool = False, default_arm: str = "both", execute: bool = False) -> int:
    cancelled = threading.Event()
    if execute and (planner != 'grok' or paired):
        raise ValueError('Live voice uses Grok and the paired follower only.')
    if planner == "grok":
        import grok_planner
        try:
            model = grok_planner.configured_model()
        except ValueError as exc:
            print(str(exc), flush=True)
            return 1
        def processor(text):
            print("TRANSCRIBED. Interpreting with Grok...", flush=True)
            import voice_control
            return voice_control.process_text(text, preview=not execute, cancelled=cancelled)
    else:
        processor = command_preview.preview
    readiness.resolve_model()
    stamp = datetime.now(ZoneInfo("Asia/Hong_Kong")).strftime("%Y-%m-%d_%H-%M-%S")
    log = TurnLog(readiness.ROOT / ".cache/logs" / f"{stamp}_voice-{time.time_ns()}.jsonl")
    log.write("session_started", planner=planner, model=model if planner == "grok" else None,
              state_source="cyberwave_edge_follower" if execute else "unavailable", execute=execute,
              paired=paired, default_arm='follower' if execute else default_arm, max_seconds=max_seconds,
              source_file=str(Path(__file__).resolve()))
    recorder = Recorder(max_seconds)
    session = Session(recorder, processor=processor, log=log)
    events = queue.Queue()
    listener = None

    def terminal_input():
        while True:
            try:
                value = input()
            except EOFError:
                events.put("quit")
                return
            if value.strip().lower() in {"quit", "exit", "q"}:
                events.put("quit")
                return
            # Global shortcuts may also leave characters in this terminal.
            # Enter must still toggle even when the input buffer is nonempty.
            events.put("toggle")

    try:
        if not terminal:
            try:
                from pynput import keyboard

                if not keyboard.Listener.IS_TRUSTED:
                    print("Global shortcut unavailable: macOS keyboard permission is missing. Using Enter instead.", flush=True)
                else:
                    shortcut = Shortcut(keyboard, lambda: events.put("toggle"), mac=sys.platform == "darwin")
                    listener = keyboard.Listener(on_press=shortcut.press, on_release=shortcut.release)
                    listener.start()
                    session.controls = f"{HOTKEY_LABEL} or Enter in this terminal"
            except Exception as exc:
                if listener is not None:
                    listener.stop()
                    listener = None
                print(f"Global shortcut unavailable ({type(exc).__name__}). Using Enter instead.", flush=True)
        print("Enter: start/stop recording in this terminal. Type quit or press Ctrl+C to exit.", flush=True)
        if planner == "grok":
            print(f"Local Whisper -> Grok ({model}) -> structured command JSON. API requests may incur charges.", flush=True)
            print("LIVE: verified directions execute through Cyberwave. Use ./robot stop in another terminal to stop the driver." if execute else
                  "Preview: exact prepared commands. No robot connection.", flush=True)
        else:
            print("Local Whisper + basic phrase helper. No LLM/API or robot.", flush=True)
        print(f"Auto-stop recording after {max_seconds:g}s.", flush=True)
        print(f"SESSION LOG: {log.path}", flush=True)
        session.ready()
        threading.Thread(target=terminal_input, daemon=True).start()
        while True:
            session.poll()
            if listener is not None and not listener.is_alive():
                try:
                    listener.join()  # Surface callback/backend errors.
                except Exception:
                    pass
                listener = None
                session.controls = "Enter"
                print("Global shortcut stopped. Enter in this terminal still starts/stops recording.", flush=True)
            try:
                event = events.get(timeout=0.05)
            except queue.Empty:
                continue
            if event == "quit":
                break
            session.toggle()
    except KeyboardInterrupt:
        pass
    finally:
        cancelled.set()
        if recorder.recording:
            session.event("recording_discarded")
        recorder.cancel()
        if listener is not None:
            listener.stop()
        if session.worker is not None and session.worker.is_alive():
            print('Cancelling the active command; waiting for stop/hold cleanup…', flush=True)
            session.worker.join(timeout=5)
        log.write("session_stopped")
        print("Stopped. Microphone off. Active command cancellation requested." if execute else "Stopped. Microphone off.", flush=True)
    return 0
