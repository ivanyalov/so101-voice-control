"""Offline helper for basic command previews, not the general agent planner.

Never reads hardware, resolves numeric motor targets or executes code.
"""

from __future__ import annotations

import re


def preview(text: str) -> dict:
    result = {
        "mode": "dry_run", "planner": "local_basic_commands",
        "status": "needs_configuration", "proposed_actions": [],
        "parameter_changes": [], "code_preview": [], "unresolved": [],
        "executed_actions": 0,
    }
    normalized = re.sub(r"[^a-z0-9'\s]", " ", text.lower())
    normalized = re.sub(r"\s+", " ", normalized).strip()
    normalized = re.sub(r"^(?:hey robot|please)\s+", "", normalized)
    clauses = re.split(r"\s+(?:and then|then)\s+", normalized)
    parsed = []
    for clause in clauses:
        match = re.fullmatch(
            r"(?:(?:move|go|turn|rise|raise|lift)(?:\s+to)?\s+)?(?:the\s+)?"
            r"(left|right|up|down)(?:\s+(?:a little|a bit))?", clause
        )
        if match:
            direction = match[1]
            joint = "shoulder_pan" if direction in {"left", "right"} else "shoulder_lift"
            parsed.append({"type": "jog_joint", "joint": joint, "direction": direction, "steps": 1})
        elif re.fullmatch(r"(?:rise|raise|lift|move|go)(?:\s+up)?\s+(?:all the way\s+)?to the top", clause):
            parsed.append({"type": "request_pose", "pose": "top", "target": None})
        elif clause == "stop":
            parsed.append({"type": "stop"})
        else:
            # Do not partially accept a sequence or extract a direction from
            # a negation, question, distance or unsupported modifier.
            result["status"] = "unsupported_command"
            result["unresolved"] = [
                "The local helper supports left/right/up/down, stop, and sequences joined by 'then'. "
                "Use agent-preview for general language; no partial command was planned."
            ]
            return result

    result["proposed_actions"] = parsed
    for index, action in enumerate(parsed, 1):
        if action["type"] == "jog_joint":
            joint, direction = action["joint"], action["direction"]
            expression = f"observed[{joint!r}] + direction_sign[{direction!r}] * jog_step[{joint!r}]"
            result["parameter_changes"].append({
                "step": index, "parameter": f"{joint}.position", "before": None,
                "after": None, "unit": None, "target_expression": expression,
            })
            result["code_preview"].append(f"# Step {index}: targets[{joint!r}] = {expression}")
        elif action["type"] == "request_pose":
            result["code_preview"].append(f"# Step {index}: targets = approved_poses['top']  # not configured")
            result["unresolved"].append(
                "'Top' needs a defined, verified pose: highest gripper position and shoulder joint limit are different targets."
            )
        else:
            result["code_preview"].append(f"# Step {index}: request cancellation; physical stop cannot be verified in this preview")
    if any(a["type"] != "stop" for a in parsed):
        result["unresolved"].append(
            "Numeric targets need fresh joint positions, verified direction mapping, units, jog sizes and calibrated limits; no hardware state was read."
        )
    else:
        result["status"] = "preview_only"
    return result
