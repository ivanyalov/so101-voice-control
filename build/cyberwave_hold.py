"""Preserve an existing calibrated SO101 pose at session boundaries.

Used by the local patch of the official Cyberwave driver. No EEPROM writes,
automatic calibration, homing, or motor tuning belong in this path.
"""
import math


def read_complete(follower, register):
    values = follower.bus.sync_read(register, normalize=False, use_sequential=True)
    if set(values) != set(follower.motors):
        raise RuntimeError(f"Incomplete motor read: {register}")
    if not all(math.isfinite(float(v)) for v in values.values()):
        raise RuntimeError(f"Invalid motor read: {register}")
    return values


def verify_calibration(follower):
    if not follower.calibration or set(follower.calibration) != set(follower.motors):
        raise RuntimeError("Existing six-motor calibration required; auto-calibration disabled")
    for register, field in (("Homing_Offset", "homing_offset"),
                            ("Min_Position_Limit", "range_min"),
                            ("Max_Position_Limit", "range_max")):
        values = read_complete(follower, register)
        for name, calibration in follower.calibration.items():
            if calibration.id != follower.motors[name].id or values[name] != getattr(calibration, field):
                raise RuntimeError(f"Calibration mismatch: {name} {register}; no registers changed")
    if any(v != 0 for v in read_complete(follower, "Operating_Mode").values()):
        raise RuntimeError("Position mode required; no motor configuration changed")


def hold_current(follower):
    positions = read_complete(follower, "Present_Position")
    follower.bus.sync_write("Goal_Position", positions, normalize=False)
    goals = read_complete(follower, "Goal_Position")
    if any(abs(goals[name] - value) > 1 for name, value in positions.items()):
        raise RuntimeError("Hold target readback failed")
    return positions


def connect_preserving_pose(follower):
    if follower.connected:
        return
    follower.bus.connect()
    follower._connected = True
    try:
        verify_calibration(follower)
        read_complete(follower, "Torque_Enable")
        hold_current(follower)
        follower.bus.enable_torque()
        if any(v != 1 for v in read_complete(follower, "Torque_Enable").values()):
            raise RuntimeError("Torque enable readback failed")
        follower._torque_enabled = True
        follower._current_positions = follower.get_observation()
    except BaseException:
        follower.bus.disconnect()
        follower._connected = False
        raise


def disconnect_preserving_pose(follower):
    if not follower.connected:
        return
    try:
        if follower._torque_enabled:
            hold_current(follower)
    finally:
        follower.bus.disconnect()
        follower._connected = False
        follower._torque_enabled = False
