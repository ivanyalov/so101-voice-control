"""Encode a single atomic STS3215 acceleration/position/speed command.

Packet layout follows Feetech's official sms_sts.WritePosEx implementation:
https://github.com/ftservo/FTServo_Python/blob/main/scservo_sdk/sms_sts.py
Only SRAM addresses 41..47 are written. No calibration or gain changes.
"""

PROFILE_SPEED = 57  # About 5 degrees/second, rather than the earlier 22 ticks/s.
PROFILE_ACCELERATION = 1  # Nonzero gentle acceleration; physical behavior needs a test.


def motion_bytes(position, *, speed=PROFILE_SPEED, acceleration=PROFILE_ACCELERATION):
    if type(position) is not int or not 0 <= position <= 4095:
        raise ValueError("Position must be an unsigned single-turn encoder value")
    if type(speed) is not int or not 1 <= speed <= 114:
        raise ValueError("Diagnostic speed must be nonzero and at most 10 degrees/second")
    if type(acceleration) is not int or not 1 <= acceleration <= 10:
        raise ValueError("Diagnostic acceleration must be nonzero and between 1 and 10")
    return [acceleration, position & 255, position >> 8, 0, 0, speed & 255, speed >> 8]


def write_profile(packet, port, motor_id, position, *, speed=PROFILE_SPEED, acceleration=PROFILE_ACCELERATION):
    payload = motion_bytes(position, speed=speed, acceleration=acceleration)
    result = packet.writeTxRx(port, motor_id, 41, len(payload), payload)
    if result != (0, 0):
        raise RuntimeError(f"Motor {motor_id} profile write failed: {result}")
