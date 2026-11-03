"""Scan a Feetech bus for motor IDs (read-only).

Usage: python scan_motors.py COM8
"""

import sys

from scservo_sdk import COMM_SUCCESS, PacketHandler, PortHandler

PORTS = sys.argv[1:] or ["COM8"]
BAUDS = [1_000_000, 500_000, 250_000, 128_000, 115_200, 57_600, 38_400, 19_200, 9_600]


def scan(port: str) -> None:
    handler = PortHandler(port)
    if not handler.openPort():
        print(f"[{port}] cannot open port")
        return

    for baud in BAUDS:
        if not handler.setBaudRate(baud):
            print(f"[{port}] cannot set baudrate {baud}")
            handler.closePort()
            return

        for proto in (0, 1):
            pkt = PacketHandler(proto)
            found = []
            for motor_id in range(0, 11):
                model, comm, _err = pkt.ping(handler, motor_id)
                if comm == COMM_SUCCESS:
                    found.append((motor_id, model))
            if found:
                print(f"[{port}] baud={baud} protocol={proto}: found {len(found)} motor(s)")
                for motor_id, model in found:
                    print(f"    id={motor_id}  model={model}")

    handler.closePort()
    print(f"[{port}] scan done")


for p in PORTS:
    scan(p)
