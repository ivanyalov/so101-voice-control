"""One bounded follower joint movement for human-observed direction mapping.

Preserves encoder calibration. Never infers direction from encoder limits.
"""
import argparse
from datetime import datetime, timezone
import json

from robot_control import RobotBus, load_config, TICKS_PER_DEGREE, MARGIN
from robot_probe import ROOT, JOINTS
import threading


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('joint', choices=JOINTS)
    parser.add_argument('--degrees', type=float, default=15,
                        help='Visible direction-calibration movement, 5 to 30 motor degrees')
    args = parser.parse_args()
    if not 5 <= args.degrees <= 30:
        parser.error('--degrees must be between 5 and 30')
    bus = RobotBus(load_config())
    result = {'joint': args.joint, 'status': 'preflight', 'executed_actions': 0,
              'attempted_actions': 0, 'completed_actions': 0, 'measured_results': []}
    try:
        bus.connect(('follower',))
        item = bus.snapshot()['follower'][args.joint]
        if item['issues']:
            raise RuntimeError('; '.join(item['issues']))
        cal, state = item['calibration'], item['state']
        start = state['present_position']
        # Permit an inward recovery of at most 12 ticks outside a saved range.
        if not cal['range_min'] - 12 <= start <= cal['range_max'] + 12:
            raise RuntimeError('Starting position outside permitted calibration tolerance')
        ticks = round(args.degrees * TICKS_PER_DEGREE)
        candidates = [start + d for d in (ticks, -ticks)
                      if cal['range_min'] + MARGIN <= start + d <= cal['range_max'] - MARGIN]
        if not candidates:
            raise RuntimeError('No room for the requested direction-calibration movement')
        target = candidates[0]
        move = dict(arm='follower', joint=args.joint, id=cal['id'], before_ticks=start,
                    target_ticks=target, delta_degrees=(target-start)/TICKS_PER_DEGREE,
                    range_min=cal['range_min'], range_max=cal['range_max'])
        result['move'] = move
        print(f"WATCH {args.joint}: {move['delta_degrees']:+.1f} motor degrees, then hold.", flush=True)
        bus.execute([[move]], threading.Event(), result)
    except BaseException as exc:
        result.update(status='failed', error=str(exc), hold_errors=bus.hold())
        raise
    finally:
        bus.close()
        folder = ROOT / '.cache/robot'
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') + '_direction_' + args.joint + '.json')
        path.write_text(json.dumps(result, indent=2) + '\n')
        print(json.dumps({'status': result['status'], 'evidence': str(path)}), flush=True)


if __name__ == '__main__':
    main()
