from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'build'))
import readiness


class CameraCheckTests(unittest.TestCase):
    def test_snapshot_returns_dimensions_and_releases_camera(self):
        camera = Mock()
        camera.read.return_value = (True, np.zeros((24, 32, 3), dtype=np.uint8))
        with tempfile.TemporaryDirectory() as directory, patch.object(readiness, 'ROOT', Path(directory)), \
             patch('cv2.VideoCapture', return_value=camera), patch('cv2.imwrite', return_value=True):
            result = readiness.camera_snapshot(0)
        self.assertEqual((result['width'], result['height']), (32, 24))
        self.assertEqual(result['cloud_calls'], 0)
        camera.release.assert_called_once()

    def test_failed_camera_read_still_releases_device(self):
        camera = Mock()
        camera.read.return_value = (False, None)
        with tempfile.TemporaryDirectory() as directory, patch.object(readiness, 'ROOT', Path(directory)), \
             patch('cv2.VideoCapture', return_value=camera), self.assertRaises(RuntimeError):
            readiness.camera_snapshot(0)
        camera.release.assert_called_once()
