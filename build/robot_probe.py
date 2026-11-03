"""Enumerate team boards; explicit --port pings IDs 1..6 without motor writes."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

from serial.tools import list_ports

ROOT = Path(__file__).resolve().parents[1]
TEAM_BOARDS = {"5B91027464": "leader", "5B91026201": "follower"}
JOINTS = ("shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll", "gripper")
# LeRobot v0.4.4 STS3215 control table. Only READ instructions are used.
READ_REGISTERS = {"firmware_major": (0, 1), "firmware_minor": (1, 1),
                  "range_min": (9, 2), "range_max": (11, 2),
                  "homing_offset": (31, 2), "operating_mode": (33, 1),
                  "torque_enabled": (40, 1), "present_position": (56, 2),
                  "voltage_tenths": (62, 1), "temperature_c": (63, 1)}


def read_registers(packet, handler, motor_id: int) -> dict:
    values = {}
    for name, (address, size) in READ_REGISTERS.items():
        reader = packet.read1ByteTxRx if size == 1 else packet.read2ByteTxRx
        value, comm, error = reader(handler, motor_id, address)
        if comm != 0 or error != 0:
            raise RuntimeError(f"Motor {motor_id}, {name}: comm={comm}, error={error}")
        if name in ("homing_offset", "present_position"):
            bit = 11 if name == "homing_offset" else 15
            value = -(value & ((1 << bit) - 1)) if value & (1 << bit) else value
        values[name] = value
    return values


def inventory() -> list[dict]:
    result = []
    for port in sorted(list_ports.comports(), key=lambda p: p.device):
        if port.vid is None:
            continue
        # Match the USB serial descriptor, never a guessed COM/tty suffix.
        role = TEAM_BOARDS.get(port.serial_number)
        result.append({"device": port.device, "serial_number": port.serial_number,
                       "vid": port.vid, "pid": port.pid, "team_role": role})
    return result


def probe(device: str, boards: list[dict]) -> dict:
    matches = [p for p in boards if p["device"] == device and p["team_role"]]
    if len(matches) != 1:
        raise ValueError("Port must currently identify one of the two team USB serial numbers")
    from scservo_sdk import COMM_SUCCESS, PacketHandler, PortHandler

    handler = PortHandler(device)
    packet = PacketHandler(0)
    motors = []
    role = matches[0]["team_role"]
    calibration_path = ROOT / "inputs/2026-11-03_so101-robot-env/calibration" / f"so101_{role}.json"
    calibration = json.loads(calibration_path.read_text())
    try:
        # Opens at the known bus baudrate, avoiding the SDK's default first open.
        if not handler.setBaudRate(1_000_000):
            raise RuntimeError("Cannot open the serial port at 1 Mbps")
        for motor_id in range(1, 7):
            model, comm, error = packet.ping(handler, motor_id)
            motors.append({"id": motor_id, "model": model if comm == COMM_SUCCESS else None,
                           "communication_result": comm, "device_error": error,
                           "ok": comm == COMM_SUCCESS and error == 0 and model == 777})
            if motors[-1]["ok"]:
                registers = read_registers(packet, handler, motor_id)
                expected = calibration[JOINTS[motor_id - 1]]
                motors[-1].update(joint=JOINTS[motor_id - 1], registers=registers,
                                 calibration_matches=all(registers[k] == expected[k]
                                                        for k in ("homing_offset", "range_min", "range_max")))
    finally:
        if handler.is_open:
            handler.closePort()
    return {"board": matches[0], "baudrate": 1_000_000, "protocol": 0,
            "motors": motors, "all_six_expected_motors": all(m["ok"] for m in motors),
            "calibration_file": str(calibration_path),
            "calibration_verified": all(m.get("calibration_matches", False) for m in motors),
            "motion_readiness_verified": False}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", help="Explicit team port to open; omitted means metadata only")
    args = parser.parse_args()
    report = {"timestamp_utc": datetime.now(timezone.utc).isoformat(),
              "mode": "ping_and_model_read" if args.port else "metadata_only",
              "motor_register_writes": 0, "executed_actions": 0, "boards": inventory()}
    exit_code = 0
    if args.port:
        try:
            report["probe"] = probe(args.port, report["boards"])
            if not report["probe"]["all_six_expected_motors"]:
                exit_code = 1
        except (OSError, RuntimeError, ValueError) as exc:
            report["error"] = str(exc)
            exit_code = 1
    directory = ROOT / ".cache/robot"
    directory.mkdir(parents=True, exist_ok=True)
    output = directory / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + ".json")
    output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({**report, "report_file": str(output)}, indent=2))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
