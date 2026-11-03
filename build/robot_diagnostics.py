"""Read-only motor settings and telemetry; no motion or configuration writes."""

from datetime import datetime, timezone
import json

from scservo_sdk import PacketHandler, PortHandler
from robot_probe import ROOT, JOINTS, inventory, read_registers

REGISTERS = {"max_voltage_tenths": (14, 1), "min_voltage_tenths": (15, 1),
             "max_torque_limit": (16, 2), "unloading_condition": (19, 1),
             "p_gain": (21, 1), "d_gain": (22, 1), "i_gain": (23, 1),
             "minimum_startup_force": (24, 2), "cw_dead_zone": (26, 1),
             "ccw_dead_zone": (27, 1), "protection_current": (28, 2),
             "acceleration": (41, 1), "goal_position": (42, 2),
             "goal_time": (44, 2), "goal_velocity": (46, 2),
             "torque_limit": (48, 2), "present_velocity": (58, 2),
             "present_load": (60, 2), "status": (65, 1),
             "moving": (66, 1), "present_current": (69, 2)}


def main():
    report = {"timestamp_utc": datetime.now(timezone.utc).isoformat(), "register_writes": 0, "arms": {}}
    packet = PacketHandler(0)
    for board in inventory():
        role = board["team_role"]
        if not role:
            continue
        handler = PortHandler(board["device"])
        report["arms"][role] = {}
        try:
            if not handler.setBaudRate(1_000_000):
                raise RuntimeError(f"Cannot open {role}")
            handler.ser.exclusive = True
            for motor_id, joint in enumerate(JOINTS, 1):
                values = read_registers(packet, handler, motor_id)
                for name, (address, size) in REGISTERS.items():
                    reader = packet.read1ByteTxRx if size == 1 else packet.read2ByteTxRx
                    value, comm, error = reader(handler, motor_id, address)
                    if comm != 0 or error != 0:
                        raise RuntimeError(f"Read {role}/{joint}/{name}: comm={comm}, error={error}")
                    values[name] = value
                report["arms"][role][joint] = values
        finally:
            if handler.is_open:
                handler.closePort()
    directory = ROOT / ".cache/robot"
    directory.mkdir(parents=True, exist_ok=True)
    output = directory / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "_diagnostics.json")
    output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({**report, "report_file": str(output)}, indent=2))


if __name__ == "__main__":
    main()
