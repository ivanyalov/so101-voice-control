"""Grok structured intent adapter for the voice system.

Returns semantic commands only; no hardware state, presets or motor targets.
"""

from __future__ import annotations

import json
import copy
import math
import os
import re

import httpx

DIRECTIONS = {
    "shoulder_pan": ("left", "right", "middle"),
    "shoulder_lift": ("up", "down"),
    "elbow_flex": ("up", "down", "forward", "backward"),
    "wrist_flex": ("up", "down"),
    "wrist_roll": ("clockwise", "counterclockwise"),
    "gripper": ("open", "close"),
}
DIRECTIONS = {joint: (*directions, 'positive', 'negative') for joint, directions in DIRECTIONS.items()}
ACTION_FIELDS = {"type", "joint", "direction", "amount_degrees", "pose"}
SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["status", "reply", "actions"],
    "properties": {
        "status": {"type": "string", "enum": ["planned", "clarify", "unsupported"]},
        "reply": {"type": "string"},
        "actions": {"type": "array", "maxItems": 48, "items": {
            "type": "object", "additionalProperties": False,
            "required": sorted(ACTION_FIELDS | {"fraction", "group", "amount_percent"}),
            "properties": {
                "type": {"type": "string", "enum": ["jog", "endpoint", "fraction", "relative", "opening", "pose", "stop"]},
                "joint": {"type": ["string", "null"], "enum": [*DIRECTIONS, None]},
                "direction": {"type": ["string", "null"], "enum": sorted({d for ds in DIRECTIONS.values() for d in ds}) + [None]},
                "amount_degrees": {"type": ["number", "null"]},
                "fraction": {"type": ["number", "null"]},
                "group": {"type": "integer", "minimum": 0, "maximum": 7},
                "amount_percent": {"type": ["number", "null"]},
                "pose": {"type": ["string", "null"]},
            },
        }},
    },
}
SYSTEM_PROMPT = """Interpret an operator's English request for an SO101 arm.
Return only the JSON intent schema. Do not calculate angles or invent hardware state.
Each action has group=0..7. Actions in the SAME group move SIMULTANEOUSLY on
one shared timeline. 'Together', 'simultaneously', 'at the same time', and 'while'
mean the same group. 'Then', 'after that', and 'followed by' start the next group.
Without explicit simultaneity, preserve listed order as separate groups. Groups
start at 0 and increase by one; never repeat a joint in the same group. Up to
8 groups and 6 different joints per group are supported.
Example 'base plus 20 degrees while elbow minus 10 degrees, then wrist roll plus
15 degrees': base relative positive 20 group=0, elbow relative negative 10 group=0,
wrist_roll relative positive 15 group=1.
The gripper uses calibrated OPENING PERCENT, not joint degrees. 'Open the gripper'
means type=opening, joint=gripper, amount_percent=100; 'close the gripper' means
amount_percent=0. 'Gripper half open' / 'open to 50 percent' means amount_percent=50.
For opening actions direction, amount_degrees, fraction and pose are all null.
For every other action amount_percent=null. Never translate gripper degrees into
opening percentage. Ask for opening percentage if the operator requests gripper degrees.
This basic joint-control convention maps plain left/right to shoulder_pan, plain
up/down to shoulder_lift. Explicit elbow means elbow_flex, never shoulder_lift.
Other joint names: wrist_flex, wrist_roll, gripper.
Base, base rotation and waist mean shoulder_pan. "Base to the left 45" means
relative/shoulder_pan/left/45. A bare numeric joint rotation amount is in degrees.
Allowed direction pairs are STRICT:
shoulder_pan: left/right; shoulder_lift: up/down;
elbow_flex: up/down/forward/backward; wrist_flex: up/down;
wrist_roll: clockwise/counterclockwise; gripper: open/close.
Every arm joint also accepts explicit coordinate directions positive/negative.
'Base plus twenty degrees' means relative/shoulder_pan/positive/20.
'Wrist roll minus fifteen degrees' means relative/wrist_roll/negative/15.
Plus/increase angle means positive; minus/decrease angle means negative.
Keep amount_degrees positive; encode the sign in direction. These are numeric
joint coordinates, not inferred left/right/clockwise physical directions.
Never use left/right for wrist_flex. Never guess an omitted movement direction.
"Move the wrist 30 degrees" is ambiguous: return status=clarify, actions=[],
reply="Bend the wrist up/down, or roll it clockwise/counterclockwise?"
"Bend the wrist down 30 degrees" is wrist_flex/down/relative/30.
"Roll the wrist clockwise 30 degrees" is wrist_roll/clockwise/relative/30.
Servo 1, motor 1, base rotation and shoulder right/left refer to shoulder_pan.
Shoulder up/down still refers to shoulder_lift; an unspecified shoulder motion is ambiguous.
For arm joints 'all the way' means endpoint in that direction, NOT a relative jog or a guessed angle.
For the gripper always use opening percentage, including fully open or fully closed.
For shoulder_pan, 'middle', 'center', 'centre' and 'back to the middle' mean
endpoint/shoulder_pan/middle with amount_degrees=null, fraction=null, pose=null.
Middle is a taught coordinate, NEVER positive/negative, zero or a guessed angle.
'Halfway right' / 'half way right' means type=fraction, joint=shoulder_pan,
direction=right, fraction=0.5, amount_degrees=null, pose=null.
'A quarter of the way left' means fraction/shoulder_pan/left/fraction=0.25.
Percentages of the taught extent use fraction, e.g. '75 percent right' is fraction=0.75.
These are absolute fractions of the taught signed endpoint coordinate, NOT a relative
move from the current location and NOT interpolation from the taught middle.
Do not calculate endpoint angles; local configuration supplies the exact coordinates.
Only type=fraction may have a non-null fraction (0 < fraction <= 1).
All other actions MUST have fraction=null. Fraction targets use left/right only.
Example 'all the way right, middle, all the way left, back to middle': exactly
four endpoint actions on shoulder_pan with directions right,middle,left,middle.
Example 'all the way left then halfway right': endpoint/shoulder_pan/left,
then fraction/shoulder_pan/right/fraction=0.5. No extra actions.
An unspecified short movement means jog (configured step, amount_degrees=null).
Only an explicitly spoken degree amount becomes relative with a positive amount_degrees.
Preserve action order for 'then'. No extra actions, automatic home or return to zero.
'Top' or 'highest gripper position' means a named pose, not a shoulder joint limit.
Use type=pose, pose='top', joint/direction/amount_degrees=null for that request.
"Go to the start position", "return to start", "starting position" and
"fold to the start position" mean exactly one type=pose action, pose='start',
joint/direction/amount_degrees=null. Never invent joint angles, a zero pose,
extra unfolding motions or a calibration action. The local saved pose is authoritative.
For stop, output only one stop action with all other fields null; never home.
For jog/endpoint/fraction/relative use a joint and a direction; pose=null.
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
    if not isinstance(actions, list) or len(actions) > 48:
        raise ValueError("Invalid action list")
    if data["status"] != "planned" and actions:
        raise ValueError("Unresolved requests cannot contain actions")
    if data["status"] == "planned" and not actions:
        raise ValueError("Planned request has no actions")
    grouped = any(isinstance(a,dict) and 'group' in a for a in actions)
    previous_group, group_joints = -1, set()
    for index, action in enumerate(actions):
        fields = ACTION_FIELDS | {"arm"} if paired else ACTION_FIELDS
        if (not isinstance(action, dict) or not fields <= set(action)
                or not set(action) <= fields | {"fraction", "group", "amount_percent"}):
            raise ValueError("Invalid action fields")
        if paired and action["arm"] not in {"leader", "follower", "both"}:
            raise ValueError("Invalid arm target")
        kind, joint, direction = action["type"], action["joint"], action["direction"]
        group = action.get('group', index)
        if ((grouped and 'group' not in action) or type(group) is not int
                or not 0 <= group < 8 or group not in {previous_group, previous_group+1}):
            raise ValueError('Invalid simultaneous group order')
        if group != previous_group:
            previous_group, group_joints = group, set()
        if joint in group_joints:
            raise ValueError('Duplicate joint in simultaneous group')
        group_joints.add(joint)
        fraction = action.get('fraction')
        percent = action.get('amount_percent')
        if kind != 'opening' and percent is not None:
            raise ValueError('Only gripper opening may specify a percentage')
        if kind != 'fraction' and fraction is not None:
            raise ValueError('Only fractional targets may include a fraction')
        if kind == 'opening':
            if (joint != 'gripper' or direction is not None or action['amount_degrees'] is not None
                    or action['pose'] is not None or not finite(percent) or not 0 <= percent <= 100):
                raise ValueError('Gripper opening needs a percentage in [0, 100]')
        elif kind in {"jog", "endpoint", "fraction", "relative"}:
            if not isinstance(joint, str) or joint not in DIRECTIONS or direction not in DIRECTIONS[joint] or action["pose"] is not None:
                raise ValueError("Invalid joint/direction")
            if direction == 'middle' and kind != 'endpoint':
                raise ValueError('Middle must be a taught endpoint target')
            if kind == 'endpoint' and direction in {'positive', 'negative'}:
                raise ValueError('Endpoint needs a named physical direction or middle')
            amount = action["amount_degrees"]
            if joint == 'gripper':
                raise ValueError('Specify gripper opening percentage, not degrees')
            if kind == "relative":
                if not finite(amount) or amount <= 0:
                    raise ValueError("Relative move needs a finite positive degree amount")
            elif kind == 'fraction':
                if joint != 'shoulder_pan' or direction not in {'left', 'right'} or not finite(fraction) or not 0 < fraction <= 1 or amount is not None:
                    raise ValueError('Fractional base target needs left/right and a fraction in (0, 1]')
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


def interpret(text: str, *, paired: bool = False, default_arm: str = "both", follower_only: bool = False) -> dict:
    model = configured_model()
    if not text.strip():
        raise ValueError("No transcript to interpret")
    if follower_only:
        paired, default_arm = True, 'follower'
        if re.search(r'\bleader\b|\bboth(?:\s+the)?\s+arms?\b|\btwo\s+arms?\b', text, re.I):
            return {'status': 'unsupported', 'reply': 'This live session controls only the follower arm.', 'actions': []}
    schema = copy.deepcopy(SCHEMA)
    prompt = SYSTEM_PROMPT
    if paired:
        if default_arm not in {"leader", "follower", "both"}:
            raise ValueError("Invalid default arm")
        item = schema["properties"]["actions"]["items"]
        item["required"].append("arm")
        item["properties"]["arm"] = {"type": "string", "enum": ["follower"] if follower_only else ["leader", "follower", "both"]}
        if follower_only:
            prompt += """
This LIVE session controls exactly ONE SO101 follower arm.
Every action MUST have arm='follower'. Omitted arm always means follower.
There is no addressable leader or both-arms target in this session.
If explicitly asked for an unavailable arm, return unsupported with actions=[].
Preserve all requested joints and their order; several joints do not mean several arms.
Do not invent physical direction signs, numeric targets, or claims of execution.
"""
        else:
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
                ], "max_tokens": 6000, "stream": False,
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
        parsed = parse_intent(choice["message"]["content"], paired=paired)
        if follower_only and any(a['arm'] != 'follower' for a in parsed['actions']):
            raise ValueError('Invalid arm target')
        return parsed
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
            'Only fractional targets may include a fraction', 'Middle must be a taught endpoint target',
            'Endpoint needs a named physical direction or middle',
            'Fractional base target needs left/right and a fraction in (0, 1]',
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
