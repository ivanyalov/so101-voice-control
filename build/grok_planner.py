"""Grok structured intent adapter for the voice system.

Returns semantic commands only; no hardware state, presets or motor targets.
"""

from __future__ import annotations

import json
import copy
import math
import os

import httpx

DIRECTIONS = {
    "shoulder_pan": ("left", "right"),
    "shoulder_lift": ("up", "down"),
    "elbow_flex": ("up", "down", "forward", "backward"),
    "wrist_flex": ("up", "down"),
    "wrist_roll": ("clockwise", "counterclockwise"),
    "gripper": ("open", "close"),
}
ACTION_FIELDS = {"type", "joint", "direction", "amount_degrees", "pose"}
SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["status", "reply", "actions"],
    "properties": {
        "status": {"type": "string", "enum": ["planned", "clarify", "unsupported"]},
        "reply": {"type": "string"},
        "actions": {"type": "array", "maxItems": 8, "items": {
            "type": "object", "additionalProperties": False,
            "required": sorted(ACTION_FIELDS),
            "properties": {
                "type": {"type": "string", "enum": ["jog", "endpoint", "relative", "pose", "stop"]},
                "joint": {"type": ["string", "null"], "enum": [*DIRECTIONS, None]},
                "direction": {"type": ["string", "null"], "enum": sorted({d for ds in DIRECTIONS.values() for d in ds}) + [None]},
                "amount_degrees": {"type": ["number", "null"]},
                "pose": {"type": ["string", "null"]},
            },
        }},
    },
}
SYSTEM_PROMPT = """Interpret an operator's English request for an SO101 arm.
Return only the JSON intent schema. Do not calculate angles or invent hardware state.
This basic joint-control convention maps plain left/right to shoulder_pan, plain
up/down to shoulder_lift. Explicit elbow means elbow_flex, never shoulder_lift.
Other joint names: wrist_flex, wrist_roll, gripper.
Allowed direction pairs are STRICT:
shoulder_pan: left/right; shoulder_lift: up/down;
elbow_flex: up/down/forward/backward; wrist_flex: up/down;
wrist_roll: clockwise/counterclockwise; gripper: open/close.
Never use left/right for wrist_flex. Never guess an omitted movement direction.
"Move the wrist 30 degrees" is ambiguous: return status=clarify, actions=[],
reply="Bend the wrist up/down, or roll it clockwise/counterclockwise?"
"Bend the wrist down 30 degrees" is wrist_flex/down/relative/30.
"Roll the wrist clockwise 30 degrees" is wrist_roll/clockwise/relative/30.
'All the way' means endpoint in that direction, NOT a relative jog or a guessed angle.
An unspecified short movement means jog (configured step, amount_degrees=null).
Only an explicitly spoken degree amount becomes relative with a positive amount_degrees.
Preserve action order for 'then'. No extra actions, automatic home or return to zero.
'Top' or 'highest gripper position' means a named pose, not a shoulder joint limit.
Use type=pose, pose='top', joint/direction/amount_degrees=null for that request.
For stop, output only one stop action with all other fields null; never home.
For jog/endpoint/relative use a joint and a direction; pose=null.
For negation, ambiguity, questions, or unsupported tasks such as picking/dancing,
return clarify/unsupported, explain briefly in reply, and actions=[]. Do not substitute a gesture.
For a planned request provide a short description, not a claim of execution.
Camera and registered picking/dance skills are unavailable in this preview.
"""


def configured_model() -> str:
    if not os.getenv("XAI_API_KEY", "").strip():
        raise ValueError("Set XAI_API_KEY locally in .env; no Grok request was made.")
    model = os.getenv("XAI_MODEL", "").strip()
    if not model:
        raise ValueError("Set XAI_MODEL in .env to a Grok model ID available to your API key.")
    if os.getenv("XAI_REASONING_EFFORT", "none") not in {"none", "low", "medium", "high", "xhigh"}:
        raise ValueError("XAI_REASONING_EFFORT must be none, low, medium, high or xhigh.")
    return model


def parse_intent(raw: str, *, paired: bool = False) -> dict:
    data = json.loads(raw)
    if not isinstance(data, dict) or set(data) != {"status", "reply", "actions"}:
        raise ValueError("Invalid intent object")
    if data["status"] not in {"planned", "clarify", "unsupported"} or not isinstance(data["reply"], str):
        raise ValueError("Invalid intent status/reply")
    actions = data["actions"]
    if not isinstance(actions, list) or len(actions) > 8:
        raise ValueError("Invalid action list")
    if data["status"] != "planned" and actions:
        raise ValueError("Unresolved requests cannot contain actions")
    if data["status"] == "planned" and not actions:
        raise ValueError("Planned request has no actions")
    for action in actions:
        fields = ACTION_FIELDS | {"arm"} if paired else ACTION_FIELDS
        if not isinstance(action, dict) or set(action) != fields:
            raise ValueError("Invalid action fields")
        if paired and action["arm"] not in {"leader", "follower", "both"}:
            raise ValueError("Invalid arm target")
        kind, joint, direction = action["type"], action["joint"], action["direction"]
        if kind in {"jog", "endpoint", "relative"}:
            if not isinstance(joint, str) or joint not in DIRECTIONS or direction not in DIRECTIONS[joint] or action["pose"] is not None:
                raise ValueError("Invalid joint/direction")
            amount = action["amount_degrees"]
            if kind == "relative":
                if not finite(amount) or amount <= 0:
                    raise ValueError("Relative move needs a finite positive degree amount")
            elif amount is not None:
                raise ValueError("Jog/endpoint cannot include an invented angle")
        elif kind in {"pose", "stop"}:
            if any(action[k] is not None for k in ("joint", "direction", "amount_degrees")):
                raise ValueError("Pose/stop has unexpected joint parameters")
            if kind == "pose" and (not isinstance(action["pose"], str) or not action["pose"].strip()):
                raise ValueError("Pose needs a name")
            if kind == "stop" and (action["pose"] is not None or len(actions) != 1):
                raise ValueError("Stop must stand alone")
        else:
            raise ValueError("Unknown action type")
    return data


def finite(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def interpret(text: str, *, paired: bool = False, default_arm: str = "both") -> dict:
    model = configured_model()
    if not text.strip():
        raise ValueError("No transcript to interpret")
    schema = copy.deepcopy(SCHEMA)
    prompt = SYSTEM_PROMPT
    if paired:
        if default_arm not in {"leader", "follower", "both"}:
            raise ValueError("Invalid default arm")
        item = schema["properties"]["actions"]["items"]
        item["required"].append("arm")
        item["properties"]["arm"] = {"type": "string", "enum": ["leader", "follower", "both"]}
        prompt += f"""
This session has TWO SO101 arms: leader and follower. Both are addressable.
Every action must have arm='leader', 'follower', or 'both'.
Explicit 'leader and follower', 'both arms' means arm='both', NOT unsupported.
If no arm is specified use arm='{default_arm}'; preserve it through subsequent 'then' actions.
Plain 'move the arm left/right' means rotate shoulder_pan, not Cartesian translation.
Preserve directional words as semantic intent. The downstream integration must define
the physical viewpoint and motor mapping. Do not invent either or numeric target angles.
For 'both arms 5 degrees left then 30 degrees right', return two relative actions,
each with arm='both', joint='shoulder_pan', and the explicitly requested amount.
Do not claim the motors are enabled or that any movement succeeded.
"""
    # No retries, server-side tools, or robot/camera data. Only the transcript
    # and fixed instruction/schema are sent to xAI when this is explicitly run.
    try:
        with httpx.Client(timeout=30, follow_redirects=False) as client:
            response = client.post(
                "https://api.x.ai/v1/chat/completions",
                headers={"Authorization": f"Bearer {os.environ['XAI_API_KEY']}"},
                json={"model": model, "messages": [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": text},
                ], "max_tokens": 1500, "stream": False,
                    "reasoning_effort": os.getenv("XAI_REASONING_EFFORT", "none"),
                    "response_format": {"type": "json_schema", "json_schema": {
                        "name": "arm_intent", "strict": True, "schema": schema,
                    }}},
            )
    except httpx.HTTPError:
        raise RuntimeError("Grok connection failed; no actions were executed.") from None
    if response.status_code != 200:
        raise RuntimeError(f"Grok HTTP {response.status_code}; check model access, key and quota. No actions executed.")
    try:
        choice = response.json()["choices"][0]
        if choice.get("finish_reason") != "stop" or choice["message"].get("refusal") or choice["message"].get("tool_calls"):
            raise ValueError("Incomplete or refused response")
        return parse_intent(choice["message"]["content"], paired=paired)
    except ValueError as exc:
        if str(exc) == 'Invalid joint/direction':
            # Do not repair an incompatible direction into a guessed motor move.
            return {"status": "clarify", "actions": [],
                    "reply": "The joint and direction were unclear. For wrist bending say up or down; for wrist roll say clockwise or counterclockwise. Which movement do you mean?"}
        # Fixed validation labels only; never expose a provider HTTP body.
        reason = str(exc) if str(exc) in {
            'Incomplete or refused response', 'Invalid intent object',
            'Invalid intent status/reply', 'Invalid action list',
            'Unresolved requests cannot contain actions', 'Planned request has no actions',
            'Invalid action fields', 'Invalid arm target',
            'Relative move needs a finite positive degree amount',
            'Jog/endpoint cannot include an invented angle',
            'Pose/stop has unexpected joint parameters', 'Pose needs a name',
            'Stop must stand alone', 'Unknown action type'} else 'Malformed JSON'
        raise ValueError(f"Grok intent rejected: {reason}; no actions executed.") from None
    except (TypeError, KeyError, IndexError):
        raise ValueError("Grok returned an incomplete response envelope; no actions executed.") from None


def resolve(intent: dict) -> dict:
    """Expose validated semantic commands without calculating motor targets."""
    paired = any("arm" in a for a in intent.get("actions", []))
    intent = parse_intent(json.dumps(intent), paired=paired)
    return {
        "mode": "intent_only", "planner": "grok", "model": os.getenv("XAI_MODEL"),
        "state_source": "unavailable", "status": intent["status"],
        "reply": intent["reply"], "proposed_actions": intent["actions"],
        "parameter_changes": [], "motion_plan": None, "executed_actions": 0,
    }


def preview(text: str, *, paired: bool = False, default_arm: str = "both") -> dict:
    return resolve(interpret(text, paired=paired, default_arm=default_arm))
