import math
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'build'))
import base_console
from workflow_protocol import trajectory


class BaseConsoleTests(unittest.TestCase):
    def test_taught_names_preserve_order_and_do_not_invent_degrees(self):
        self.assertEqual(base_console.parse('servo 1 all the way right then all the way left then stop at the middle'), ['right', 'left', 'middle'])
        with self.assertRaises(ValueError):
            base_console.parse('servo 2 right')

    def test_full_travel_is_retained_and_peak_speed_is_limited(self):
        start, target = 66.5934065934066, -66.5934065934066
        steps, duration = trajectory(math.radians(start),
            {'type':'absolute','joint':'_1','target_radians':math.radians(target)},
            [math.radians(target),math.radians(start)])
        self.assertAlmostEqual(math.degrees(steps[-1]), target)
        self.assertLessEqual(max(abs(math.degrees(b-a))*30 for a,b in zip(steps,steps[1:])), 45)
        self.assertAlmostEqual(duration, (len(steps)-1)/30)


if __name__ == '__main__':
    unittest.main()
