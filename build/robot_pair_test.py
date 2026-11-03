"""Bounded two-arm bring-up: read-only plan by default; --execute moves 5 degrees.

Uses the team's pinned Feetech SDK and LeRobot v0.4.4 register definitions.
No ID, calibration, firmware or EEPROM writes. Successful execution leaves the
motors holding the final pose. Support the arms before turning off their power.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import time

from scservo_sdk import COMM_SUCCESS, PacketHandler, PortHandler
from robot_probe import ROOT, JOINTS, inventory, read_registers
from servo_motion import write_profile, PROFILE_SPEED, PROFILE_ACCELERATION

TICKS = 57  # 5.01 motor-coordinate degrees at 4095 ticks/revolution.
VELOCITY = PROFILE_SPEED
MARGIN = 16
RAM = {"torque": (40, 1), "goal": (42, 2), "time": (44, 2), "speed": (46, 2)}


def checked(result, context):
    if result[-2:] != (COMM_SUCCESS, 0):
        raise RuntimeError(f"{context}: communication/device error {result[-2:]}")
    return result[0] if len(result) == 3 else None


def make_plan(arms: dict) -> list[dict]:
    if set(arms) != {"leader", "follower"}:
        raise ValueError("Exactly one leader and one follower are required")
    moves = []
    for joint in JOINTS:
        candidates = []
        for sign in (1, -1):
            group = []
            for role in ("follower", "leader"):
                item = arms[role][joint]
                cal, state = item["calibration"], item["state"]
                start, target = state["present_position"], state["present_position"] + sign * TICKS
                if not cal["range_min"] + MARGIN <= target <= cal["range_max"] - MARGIN:
                    break
                group.append({"role": role, "joint": joint, "id": cal["id"],
                              "start": start, "target": target, "delta_ticks": sign * TICKS,
                              "range_min": cal["range_min"], "range_max": cal["range_max"]})
            if len(group) == 2:
                score = sum(abs(m["target"] - (m["range_min"] + m["range_max"]) / 2) for m in group)
                candidates.append((score, group))
        if not candidates:
            raise ValueError(f"No common bounded 5-degree direction for {joint}; reposition manually")
        moves.extend(min(candidates, key=lambda item: item[0])[1])
    return moves


class PairTest:
    def __init__(self):
        self.ports = {}
        self.packet = PacketHandler(0)
        self.attempted = []
        self.writes = []
        self.samples = []

    def open_and_read(self, *, require_torque_off: bool = True) -> dict:
        boards = [b for b in inventory() if b["team_role"]]
        if len(boards) != 2 or {b["team_role"] for b in boards} != {"leader", "follower"}:
            raise RuntimeError("Both team boards must be connected")
        arms = {}
        for board in boards:
            role = board["team_role"]
            handler = PortHandler(board["device"])
            self.ports[role] = handler
            if not handler.setBaudRate(1_000_000):
                raise RuntimeError(f"Cannot open {role}")
            handler.ser.exclusive = True
            calibration = json.loads((ROOT / "inputs/2026-11-03_so101-robot-env/calibration" /
                                           f"so101_{role}.json").read_text())
            arms[role] = {}
            firmware = set()
            for joint in JOINTS:
                cal = calibration[joint]
                model = checked(self.packet.ping(handler, cal["id"]), f"{role}/{joint}/model")
                if model != 777:
                    raise RuntimeError(f"Unexpected motor model on {role}/{joint}")
                state = read_registers(self.packet, handler, cal["id"])
                if any(state[k] != cal[k] for k in ("range_min", "range_max", "homing_offset")):
                    raise RuntimeError(f"Calibration mismatch on {role}/{joint}")
                if (require_torque_off and state["torque_enabled"] != 0) or state["operating_mode"] != 0:
                    raise RuntimeError(f"{role}/{joint} must be idle, torque off, position mode")
                if not 105 <= state["voltage_tenths"] <= 135 or state["temperature_c"] > 55:
                    raise RuntimeError(f"Power/temperature check failed on {role}/{joint}")
                firmware.add((state["firmware_major"], state["firmware_minor"]))
                arms[role][joint] = {"calibration": cal, "state": state}
            if len(firmware) != 1:
                raise RuntimeError(f"Mixed firmware on {role}")
        return arms

    def read(self, role, motor_id, address):
        return checked(self.packet.read2ByteTxRx(self.ports[role], motor_id, address),
                       f"{role}/{motor_id}/read/{address}")

    def position(self, role, motor_id):
        value = self.read(role, motor_id, 56)
        return -(value & 32767) if value & 32768 else value

    def write(self, role, motor_id, name, value):
        address, size = RAM[name]
        writer = self.packet.write1ByteTxRx if size == 1 else self.packet.write2ByteTxRx
        self.writes.append({"role": role, "id": motor_id, "register": name, "value": value})
        checked(writer(self.ports[role], motor_id, address, value), f"{role}/{motor_id}/{name}")

    def hold(self) -> list[str]:
        errors = []
        for move in self.attempted:
            try:
                measured = self.position(move["role"], move["id"])
                # Firmware accepts only targets within the calibrated range.
                target = min(move["range_max"], max(move["range_min"], measured))
                self.write(move["role"], move["id"], "goal", target)
            except Exception as exc:
                errors.append(str(exc))
        return errors

    def execute(self, moves: list[dict]) -> dict:
        # Acknowledging commands/calibration does not establish drive readiness.
        # Check every requested motor before any motion/configuration write.
        for move in moves:
            role, motor_id = move["role"], move["id"]
            gain = checked(self.packet.read1ByteTxRx(self.ports[role], motor_id, 21), f"{role}/{motor_id}/P gain")
            limit = self.read(role, motor_id, 48)
            if gain == 0 or limit == 0:
                raise RuntimeError(f"{role}/{move['joint']}: zero drive gain or torque limit; restore verified motor configuration before movement")
        # Re-read every start before the first write, so manual repositioning
        # after the snapshot cannot silently alter the permitted displacement.
        for move in moves:
            if abs(self.position(move["role"], move["id"]) - move["start"]) > 3:
                raise RuntimeError("An arm moved after preflight; make a fresh plan")
        for move in moves:
            role, motor_id = move["role"], move["id"]
            # The return command may start with torque already on. Track a goal
            # before attempting its write so interruption always holds it too.
            self.attempted.append(move)
            self.writes.append({"role": role, "id": motor_id, "register": "motion_profile_41_47",
                                "value": {"acceleration": PROFILE_ACCELERATION, "target": move["target"], "speed": VELOCITY}})
            write_profile(self.packet, self.ports[role], motor_id, move["target"])
            if self.read(role, motor_id, 46) != VELOCITY or self.read(role, motor_id, 42) != move["target"]:
                raise RuntimeError("Motor did not accept the bounded target/speed")
        enabled_at = []
        started = time.monotonic()
        for move in moves:
            self.write(move["role"], move["id"], "torque", 1)
            enabled_at.append(time.monotonic())
        samples = self.samples
        stable = 0
        deadline_seconds = max(7, max(abs(m["target"] - m["start"]) for m in moves) / VELOCITY + 4)
        while time.monotonic() - started < deadline_seconds:
            sample = []
            for move in moves:
                pos = self.position(move["role"], move["id"])
                low, high = sorted((move["start"], move["target"]))
                if not low - 20 <= pos <= high + 20:
                    raise RuntimeError(f"Unexpected displacement on {move['role']}/{move['joint']}")
                sample.append({"role": move["role"], "joint": move["joint"], "position": pos,
                               "target_error_ticks": pos - move["target"]})
            samples.append({"elapsed_seconds": round(time.monotonic() - started, 3), "motors": sample})
            stable = stable + 1 if all(abs(p["target_error_ticks"]) <= 12 for p in sample) else 0
            if stable >= 3:
                return {"status": "reached_targets_holding", "samples": samples,
                        "enable_spread_ms": round((max(enabled_at) - min(enabled_at)) * 1000, 2),
                        "motors_energized": True}
            time.sleep(0.05)
        raise RuntimeError("Not all motors reached their targets before timeout")

    def close(self):
        for handler in self.ports.values():
            if handler.is_open:
                handler.closePort()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="Energize all 12 motors and perform the test")
    args = parser.parse_args()
    test = PairTest()
    report = {"timestamp_utc": datetime.now(timezone.utc).isoformat(),
              "mode": "execute" if args.execute else "read_only_plan", "eeprom_writes": 0}
    code = 0
    try:
        report["snapshot"] = test.open_and_read()
        report["moves"] = make_plan(report["snapshot"])
        if args.execute:
            report["result"] = test.execute(report["moves"])
        else:
            report["status"] = "planned_no_motion"
    except (Exception, KeyboardInterrupt) as exc:
        report.update(status="failed_or_interrupted", error=str(exc) or "KeyboardInterrupt")
        report["hold_errors"] = test.hold()
        report["motors_may_be_energized"] = bool(test.attempted)
        code = 1
    finally:
        test.close()
        report["register_writes"] = test.writes
        report["samples"] = test.samples
        directory = ROOT / ".cache/robot"
        directory.mkdir(parents=True, exist_ok=True)
        output = directory / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "_pair-test.json")
        output.write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps({"mode": report["mode"], "status": report.get("status", report.get("result", {}).get("status")),
                          "error": report.get("error"), "hold_errors": report.get("hold_errors"),
                          "moves": report.get("moves"), "result": report.get("result", {}).get("samples", [])[-1:],
                          "report_file": str(output)}, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
