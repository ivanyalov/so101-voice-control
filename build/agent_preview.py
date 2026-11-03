"""Reuse Cyberwave's multimodal planner with local speech. No executor exists here."""

from __future__ import annotations

import json
import os

from planner import plan_from_utterance, plan_from_utterance_with_image
import readiness


def run(*, voice: bool = False, vision: bool = False, speak: bool = False, seconds: float = 4) -> int:
    if not os.getenv("ANTHROPIC_API_KEY"):
        raise SystemExit("Set ANTHROPIC_API_KEY in .env before using the cloud planner.")
    if voice:
        readiness.resolve_model()
    camera = None
    if vision:
        from vision import open_camera_from_env

        camera = open_camera_from_env()
    print("Agent preview: local speech, cloud planner, NO robot connection or executor.")
    print("Type quit to exit. Model requests may incur provider charges.")
    try:
        while True:
            try:
                value = input("Press Enter to record, or type a command: " if voice else "Command: ").strip()
            except EOFError:
                break
            if value.lower() in {"quit", "exit", "bye"}:
                break
            try:
                if voice and not value:
                    transcript = readiness.transcribe(readiness.record(seconds))
                    value = transcript["text"]
                    print(f"Heard: {value}")
                if not value:
                    continue
                frame = camera.grab_frame_b64() if camera else None
                if camera and frame is None:
                    reply = "I can't inspect the scene because the camera frame is unavailable."
                    print(reply)
                    if speak:
                        readiness.synthesize(reply)
                    continue
                result = (plan_from_utterance_with_image(value, frame) if camera else plan_from_utterance(value))
                if not result.ok or result.plan is None:
                    print("Planner returned an invalid plan; nothing was executed.")
                    continue
                print(json.dumps({"reply": result.plan.say, "proposed_actions": [vars(a) for a in result.plan.actions], "executed_actions": 0}, indent=2))
                if speak and result.plan.say:
                    prefix = "Preview only. " if result.plan.actions else ""
                    readiness.synthesize(prefix + result.plan.say)
            except Exception as exc:
                # Do not dump HTTP exception bodies, which may contain request context.
                print(f"Turn failed ({type(exc).__name__}); nothing was executed.")
    except KeyboardInterrupt:
        print("\nPreview stopped.")
    finally:
        if camera:
            camera.close()
    return 0
