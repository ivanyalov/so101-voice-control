import importlib.util
from pathlib import Path
import socket
import sys
import types
import unittest
from unittest.mock import Mock, patch


class BridgeTests(unittest.TestCase):
    def test_existing_bridge_accepted_socket_disables_nagle(self):
        macos = types.ModuleType('cyberwave_cli.macos')
        package = types.ModuleType('cyberwave_cli')
        package.macos = macos
        with patch.dict(sys.modules, {'cyberwave_cli': package, 'cyberwave_cli.macos': macos}):
            path = Path(__file__).resolve().parents[1] / 'build/cyberwave_bridge.py'
            spec = importlib.util.spec_from_file_location('test_bridge_module', path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        connection = Mock()
        self.assertIs(module.enable_low_latency(connection), connection)
        connection.setsockopt.assert_called_once_with(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)


if __name__ == '__main__':
    unittest.main()
