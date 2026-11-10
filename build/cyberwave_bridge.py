"""Run the existing Cyberwave Mac bridge with low-latency accepted sockets.

Uses the installed bridge unchanged apart from TCP_NODELAY on its accepted
connection. No additional serial controller, protocol, or motor commands.
"""
import socket
import sys

from cyberwave_cli import macos


def enable_low_latency(conn):
    conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    return conn


class BridgeSocket(socket.socket):
    def accept(self):
        conn, address = super().accept()
        return enable_low_latency(conn), address


def main():
    # The installed server constructs its listener at runtime. Keep its serial
    # pump, baud handling, per-connection device open and shutdown behavior.
    macos.socket.socket = BridgeSocket
    return macos.main(sys.argv[1:])


if __name__ == '__main__':
    raise SystemExit(main())
