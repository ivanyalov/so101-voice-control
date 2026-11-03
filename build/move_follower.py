"""Move only the team follower's base by five degrees, then hold position."""

import json
import time

from scservo_sdk import PacketHandler, PortHandler
from robot_probe import ROOT, inventory, read_registers
from servo_motion import write_profile


def main():
    boards = [b for b in inventory() if b["team_role"] == "follower"]
    if len(boards) != 1:
        raise RuntimeError("Follower USB board 5B91026201 is not connected")
    calibration = json.loads((ROOT / "inputs/2026-11-03_so101-robot-env/calibration/so101_follower.json").read_text())["shoulder_pan"]
    handler, packet = PortHandler(boards[0]["device"]), PacketHandler(0)
    moving = False

    def read_position():
        raw, comm, error = packet.read2ByteTxRx(handler, 1, 56)
        if comm != 0 or error != 0:
            raise RuntimeError(f"Position read failed: comm={comm}, error={error}")
        return -(raw & 32767) if raw & 32768 else raw

    def write(address, value, size=2):
        writer = packet.write1ByteTxRx if size == 1 else packet.write2ByteTxRx
        comm, error = writer(handler, 1, address, value)
        if comm != 0 or error != 0:
            raise RuntimeError(f"Motor write failed: comm={comm}, error={error}")

    try:
        if not handler.setBaudRate(1_000_000):
            raise RuntimeError("Cannot open follower port")
        handler.ser.exclusive = True
        model, comm, error = packet.ping(handler, 1)
        if (model, comm, error) != (777, 0, 0):
            raise RuntimeError("Unexpected follower base motor")
        state = read_registers(packet, handler, 1)
        if any(state[k] != calibration[k] for k in ("homing_offset", "range_min", "range_max")):
            raise RuntimeError("Follower calibration mismatch")
        if state["operating_mode"] != 0 or not 105 <= state["voltage_tenths"] <= 135 or state["temperature_c"] > 55:
            raise RuntimeError("Motor mode, power or temperature check failed")
        gain, comm, error = packet.read1ByteTxRx(handler, 1, 21)
        if comm != 0 or error != 0 or gain == 0:
            raise RuntimeError("Base motor control gain is zero or unreadable")
        limit, comm, error = packet.read2ByteTxRx(handler, 1, 48)
        if comm != 0 or error != 0 or limit == 0:
            raise RuntimeError("Base motor torque limit is zero or unreadable")
        start = state["present_position"]
        target = start + 57
        if not calibration["range_min"] + 16 <= target <= calibration["range_max"] - 16:
            raise RuntimeError("Five-degree target is too close to a calibrated limit")
        # Cancel a stale target before enabling torque. If torque is already on,
        # it remains on; only this motor's bounded target is changed.
        moving = True
        write_profile(packet, handler, 1, start)
        print("Follower base: 5-degree test, nonzero acceleration, speed cap about 5 degrees/s. Ctrl+C stops and holds.", flush=True)
        write(40, 1, 1)
        write_profile(packet, handler, 1, target)
        deadline, stable = time.monotonic() + 7, 0
        while time.monotonic() < deadline:
            measured = read_position()
            if abs(measured - start) > 77:
                raise RuntimeError("Unexpected base displacement")
            stable = stable + 1 if abs(measured - target) <= 12 else 0
            if stable >= 3:
                moving = False
                print(f"Done. Measured base rotation: {(measured - start) * 360 / 4095:.1f} degrees. Holding position.")
                return
            time.sleep(0.05)
        raise RuntimeError("Motor did not reach its target within seven seconds")
    finally:
        if moving:
            try:
                write(42, read_position())
                print("Stopped; holding measured position.", flush=True)
            except Exception as exc:
                print(f"Could not confirm motor stop: {exc}", flush=True)
        if handler.is_open:
            handler.closePort()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        raise SystemExit(str(exc))
