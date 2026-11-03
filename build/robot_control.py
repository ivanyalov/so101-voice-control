"""Measured two-arm planning and bounded Feetech execution; no generated code eval.

Only RAM torque/goal/time/speed writes are allowed. Calibration/firmware and
controller gains are never changed. Direction signs must be physically verified.
Register reference: LeRobot v0.4.4 motors/feetech/tables.py.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import threading
import time

import grok_planner
from servo_motion import write_profile, PROFILE_SPEED
from robot_probe import ROOT, JOINTS, inventory, read_registers

PROFILE = ROOT / "config/robot-control.json"
RAM = {"torque": (40, 1), "goal": (42, 2), "time": (44, 2), "speed": (46, 2)}
EXTRA = {"max_torque": (16, 2), "position_p": (21, 1), "goal": (42, 2),
         "speed": (46, 2), "torque_limit": (48, 2), "status": (65, 1)}
TICKS_PER_DEGREE = 4095 / 360
MARGIN = 16
TOLERANCE = 12


def checked(result, context):
    if result[-2:] != (0, 0):
        raise RuntimeError(f"{context}: communication/device error {result[-2:]}")
    return result[0] if len(result) == 3 else None


def health_issues(role, joint, cal, state):
    prefix = f"{role}/{joint}"
    issues = []
    if any(state[k] != cal[k] for k in ("homing_offset", "range_min", "range_max")):
        issues.append(f"{prefix}: calibration mismatch")
    if state["operating_mode"] != 0:
        issues.append(f"{prefix}: not in position mode")
    if not 105 <= state["voltage_tenths"] <= 135 or not 0 <= state["temperature_c"] <= 55:
        issues.append(f"{prefix}: voltage/temperature outside permitted bounds")
    if state["max_torque"] <= 0 or state["torque_limit"] <= 0 or state["position_p"] <= 0:
        issues.append(f"{prefix}: zero torque limit or position P gain; active motion unavailable")
    if state["status"] != 0:
        issues.append(f"{prefix}: motor status={state['status']}")
    return issues


class RobotBus:
    def __init__(self, config):
        from scservo_sdk import PacketHandler
        self.config = config
        self.packet = PacketHandler(0)
        self.ports = {}
        self.touched = {}
        self.write_count = 0

    def connect(self, roles):
        from scservo_sdk import PortHandler
        boards = inventory()
        try:
            for role in roles:
                serial = self.config["arms"][role]["serial"]
                matches = [b for b in boards if b["serial_number"] == serial and b["team_role"] == role]
                if len(matches) != 1:
                    raise RuntimeError(f"Exactly one {role} board with serial {serial} is required")
                handler = PortHandler(matches[0]["device"])
                self.ports[role] = handler
                if not handler.setBaudRate(1_000_000):
                    raise RuntimeError(f"Cannot open {role}")
                handler.ser.exclusive = True
                # Match the upstream LeRobot workaround for the PyPI SDK timeout.
                def timeout(length, h=handler):
                    h.packet_start_time = h.getCurrentTime()
                    h.packet_timeout = h.tx_time_per_byte * (length + 3) + 50
                handler.setPacketTimeout = timeout
        except BaseException:
            self.close()
            raise

    def snapshot(self):
        arms = {}
        for role, handler in self.ports.items():
            path = ROOT / self.config["arms"][role]["calibration_file"]
            calibration = json.loads(path.read_text())
            arms[role] = {}
            firmware = set()
            for motor_id, joint in enumerate(JOINTS, 1):
                cal = calibration[joint]
                if cal["id"] != motor_id or cal["drive_mode"] not in (0, 1):
                    raise RuntimeError(f"Unexpected calibration mapping: {role}/{joint}")
                if checked(self.packet.ping(handler, motor_id), f"{role}/{joint}/model") != 777:
                    raise RuntimeError(f"Unexpected motor model: {role}/{joint}")
                state = read_registers(self.packet, handler, motor_id)
                for name, (address, size) in EXTRA.items():
                    state[name] = self.read(role, motor_id, address, size)
                arms[role][joint] = {"calibration": cal, "state": state,
                                      "issues": health_issues(role, joint, cal, state)}
                firmware.add((state["firmware_major"], state["firmware_minor"]))
            if len(firmware) != 1:
                raise RuntimeError(f"Mixed firmware on {role}")
        return arms

    def read(self, role, motor_id, address, size=2):
        reader = self.packet.read1ByteTxRx if size == 1 else self.packet.read2ByteTxRx
        return checked(reader(self.ports[role], motor_id, address), f"{role}/{motor_id}/read/{address}")

    def position(self, role, motor_id):
        value = self.read(role, motor_id, 56)
        return -(value & 32767) if value & 32768 else value

    def write(self, role, motor_id, name, value):
        address, size = RAM[name]  # No arbitrary register, EEPROM or gain write path.
        self.write_count += 1  # Count attempts; a missing ACK may still mean a write occurred.
        writer = self.packet.write1ByteTxRx if size == 1 else self.packet.write2ByteTxRx
        checked(writer(self.ports[role], motor_id, address, value), f"{role}/{motor_id}/{name}")

    def hold(self):
        errors = []
        for (role, motor_id), move in self.touched.items():
            try:
                position = self.position(role, motor_id)
                # Refuse an unsafe hold target rather than clamping into motion.
                if not move["range_min"] <= position <= move["range_max"]:
                    raise RuntimeError(f"{role}/{motor_id}: measured hold target outside range")
                self.write(role, motor_id, "goal", position)
            except Exception as exc:
                errors.append(str(exc))
        return errors

    def profile(self, role, motor_id, target):
        self.write_count += 1
        write_profile(self.packet, self.ports[role], motor_id, target)

    def execute(self, steps, stop, result):
        # Full plan was validated before this method. Check all starts before ANY write.
        first = {}
        for step in steps:
            for move in step:
                first.setdefault((move["arm"], move["id"]), move)
        for (role, motor_id), move in first.items():
            if abs(self.position(role, motor_id) - move["before_ticks"]) > 3:
                raise RuntimeError("Arm moved after planning; request a fresh plan")
        for step in steps:
            if stop.is_set():
                raise InterruptedError("Operator requested stop")
            for move in step:
                role, motor_id = move["arm"], move["id"]
                current = self.position(role, motor_id)
                if abs(current - move["before_ticks"]) > TOLERANCE:
                    raise RuntimeError(f"Unexpected position before {role}/{move['joint']}")
                self.touched[(role, motor_id)] = move  # Include even a lost write ACK.
                # Clear stale goal before enabling torque; never enable other joints.
                if stop.is_set():
                    raise InterruptedError("Operator requested stop")
                self.profile(role, motor_id, current)
                self.write(role, motor_id, "torque", 1)
            for move in step:
                if stop.is_set():
                    raise InterruptedError("Operator requested stop")
                result["attempted_actions"] += 1
                self.profile(move["arm"], move["id"], move["target_ticks"])
                result["executed_actions"] += 1
            # Two USB buses are dispatched consecutively; no hard real-time sync claim.
            deadline = time.monotonic() + max(abs(m["delta_degrees"]) for m in step) / (PROFILE_SPEED / TICKS_PER_DEGREE) + 5
            stable = 0
            measured = []
            while time.monotonic() < deadline:
                if stop.is_set():
                    raise InterruptedError("Operator requested stop")
                measured = []
                for move in step:
                    role, motor_id = move["arm"], move["id"]
                    pos = self.position(role, motor_id)
                    low = min(move["before_ticks"], move["target_ticks"]) - TOLERANCE
                    high = max(move["before_ticks"], move["target_ticks"]) + TOLERANCE
                    if not low <= pos <= high:
                        raise RuntimeError(f"Unexpected displacement on {role}/{move['joint']}")
                    status = self.read(role, motor_id, 65, 1)
                    voltage = self.read(role, motor_id, 62, 1)
                    temperature = self.read(role, motor_id, 63, 1)
                    if status or not 105 <= voltage <= 135 or not 0 <= temperature <= 55:
                        raise RuntimeError(f"Motor health check failed on {role}/{move['joint']}")
                    measured.append({"arm": role, "joint": move["joint"], "position_ticks": pos,
                                     "target_error_ticks": pos - move["target_ticks"]})
                stable = stable + 1 if all(abs(m["target_error_ticks"]) <= TOLERANCE for m in measured) else 0
                if stable >= 3:
                    result["completed_actions"] += len(step)
                    result["measured_results"].extend(measured)
                    break
                stop.wait(0.05)
            else:
                raise RuntimeError("Target not reached before timeout; remaining actions cancelled")
        result["status"] = "executed_holding"

    def close(self):
        for handler in self.ports.values():
            if handler.is_open:
                handler.closePort()
        self.ports.clear()


def load_config(path=PROFILE):
    config = json.loads(Path(path).read_text())
    if config.get("schema_version") != 1 or set(config.get("arms", {})) != {"leader", "follower"}:
        raise ValueError("Invalid robot-control configuration")
    return config


def resolve(intent, snapshot, config, *, execute=False):
    intent = grok_planner.parse_intent(json.dumps(intent), paired=True)
    result = {"mode": "execute" if execute else "hardware_preview", "planner": "grok",
              "state_source": "measured", "status": intent["status"], "reply": intent["reply"],
              "proposed_actions": intent["actions"], "parameter_changes": [], "motion_plan": None,
              "code_preview": [], "unresolved": [], "execution_blockers": [],
              "executed_actions": 0, "attempted_actions": 0, "completed_actions": 0, "measured_results": []}
    if intent["status"] != "planned":
        return result, []
    positions = {(r, j): v["state"]["present_position"] for r, arm in snapshot.items() for j, v in arm.items()}
    steps, total_travel = [], {}
    for index, action in enumerate(intent["actions"], 1):
        if action["type"] == "stop":
            result["status"] = "stop_requested"
            return result, []
        if action["type"] not in {"relative", "jog"}:
            result["unresolved"].append(f"Step {index}: execution supports relative/jog joint moves; endpoints and poses require separate validation")
            continue
        joint = action["joint"]
        roles = ("follower", "leader") if action["arm"] == "both" else (action["arm"],)
        group = []
        for role in roles:
            if role not in snapshot:
                result["unresolved"].append(f"{role}: board not connected")
                continue
            item = snapshot[role][joint]
            cal, state = item["calibration"], item["state"]
            result["execution_blockers"].extend(item["issues"])
            settings = config["arms"][role].get("joints", {}).get(joint, {})
            sign = settings.get("direction_sign", {}).get(action["direction"])
            if settings.get("verified") is not True or type(sign) is not int or sign not in (-1, 1):
                result["unresolved"].append(f"{role}/{joint}: physical direction '{action['direction']}' is not verified")
                continue
            amount = 5 if action["type"] == "jog" else action["amount_degrees"]
            cap = 60 if joint == "gripper" else 30
            if not math.isfinite(amount) or not 0 < amount <= cap:
                result["unresolved"].append(f"Step {index}: maximum relative movement for {joint} is {cap} motor degrees")
                continue
            delta = round(sign * amount * TICKS_PER_DEGREE)
            before = positions[(role, joint)]
            target = before + delta
            total_travel[(role, joint)] = total_travel.get((role, joint), 0) + abs(delta)
            # A measured start at the edge can move inward to a valid target.
            # Up to TOLERANCE ticks outside is readback/settling tolerance only;
            # every commanded target must still be inside the margin.
            if (not cal["range_min"] - TOLERANCE <= before <= cal["range_max"] + TOLERANCE
                    or not cal["range_min"] + MARGIN <= target <= cal["range_max"] - MARGIN
                    or total_travel[(role, joint)] > 60 * TICKS_PER_DEGREE):
                result["unresolved"].append(f"Step {index}: {role}/{joint} exceeds calibrated limits or 60-degree total travel cap")
                continue
            move = {"step": index, "arm": role, "joint": joint, "id": cal["id"],
                    "before_ticks": before, "target_ticks": target, "delta_degrees": delta / TICKS_PER_DEGREE,
                    "range_min": cal["range_min"], "range_max": cal["range_max"]}
            group.append(move)
            positions[(role, joint)] = target
            result["parameter_changes"].append(move)
        steps.append(group)
    result["execution_blockers"] = sorted(set(result["execution_blockers"]))
    if result["unresolved"]:
        result["status"] = "needs_configuration"
        return result, []
    result["motion_plan"] = {"steps": steps, "speed_degrees_per_second": PROFILE_SPEED / TICKS_PER_DEGREE,
                             "angle_units": "motor degrees; gripper jaw aperture is not calibrated"}
    result["code_preview"] = [f"bus.write({m['arm']!r}, {m['id']}, 'goal', {m['target_ticks']})  # validated/interpolated executor only"
                              for group in steps for m in group]
    result["status"] = "blocked_hardware" if result["execution_blockers"] else "resolved_measured"
    result["validation"] = "Per-arm calibration, measured position and numeric limits checked. Collision geometry is not modelled."
    return result, steps


class Controller:
    def __init__(self, *, execute=False, default_arm="both", config=None):
        self.execute = execute
        self.default_arm = default_arm
        self.config = load_config() if config is None else config
        self.cancel = threading.Event()
        self.motion_lock = threading.Lock()

    def stop(self):
        self.cancel.set()

    def process(self, text):
        # Voice loop clears cancellation before recording, never after STT/HTTP.
        if self.cancel.is_set():
            return {"status": "cancelled", "executed_actions": 0}
        intent = grok_planner.interpret(text, paired=True, default_arm=self.default_arm)
        if self.cancel.is_set():
            return {"status": "cancelled", "executed_actions": 0, "mode": "execute" if self.execute else "hardware_preview"}
        return self.process_intent(intent)

    def process_intent(self, intent):
        intent = grok_planner.parse_intent(json.dumps(intent), paired=True)
        base = {"mode": "execute" if self.execute else "hardware_preview", "state_source": "not_read",
                    "planner": "grok", "reply": intent["reply"], "proposed_actions": intent["actions"],
                "status": intent["status"], "executed_actions": 0, "register_write_attempts": 0,
                "model": os.getenv("XAI_MODEL")}
        if intent["status"] != "planned":
            return base
        if intent["actions"][0]["type"] == "stop":
            self.stop()
            return {**base, "status": "stop_requested", "reply": "No new motion; active movement is stopped through the terminal stop control."}
        roles = {r for a in intent["actions"] for r in (("leader", "follower") if a["arm"] == "both" else (a["arm"],))}
        bus = RobotBus(self.config)
        result = base
        try:
            with self.motion_lock:
                bus.connect(sorted(roles))
                snapshot = bus.snapshot()
                result, steps = resolve(intent, snapshot, self.config, execute=self.execute)
                result["measured_positions_ticks"] = {r: {j: v["state"]["present_position"] for j, v in arm.items()}
                                                       for r, arm in snapshot.items()}
                if self.execute and result["status"] == "resolved_measured":
                    # Print the exact validated plan before any actuator writes.
                    print("VALIDATED PLAN (before motion): " + json.dumps(result["motion_plan"]), flush=True)
                    bus.execute(steps, self.cancel, result)
        except (Exception, KeyboardInterrupt) as exc:
            result.update(status="cancelled" if isinstance(exc, (InterruptedError, KeyboardInterrupt)) else "hardware_failed",
                          error=str(exc) or "KeyboardInterrupt")
            if bus.touched:
                result["hold_errors"] = bus.hold()
                result["motors_may_be_energized"] = True
        finally:
            result["register_write_attempts"] = bus.write_count
            bus.close()
        return result


def diagnostics(config=None):
    config = load_config() if config is None else config
    bus = RobotBus(config)
    result = {"mode": "read_only_diagnostics", "executed_actions": 0, "register_write_attempts": 0}
    try:
        bus.connect(("leader", "follower"))
        result["arms"] = bus.snapshot()
        result["hardware_issues"] = [i for arm in result["arms"].values() for item in arm.values() for i in item["issues"]]
        result["direction_issues"] = [f"{r}/{j}: operator-view direction not verified" for r, arm in config["arms"].items()
                                      for j in JOINTS if arm.get("joints", {}).get(j, {}).get("verified") is not True]
        result["ready_for_execution"] = not result["hardware_issues"] and not result["direction_issues"]
    finally:
        bus.close()
    return result
