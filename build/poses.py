"""Private, twin-bound measured poses. Saved angles are not collision-free paths."""
import json
import math
import os
from pathlib import Path
import re
import time

import robot


def path():
    return robot.ROOT / '.cache/robot/poses.json'


def load_pose(name, twin):
    if not re.fullmatch(r'[a-z][a-z0-9_-]{0,39}', name):
        raise ValueError('Invalid pose name.')
    try:
        data = json.loads(path().read_text())
    except FileNotFoundError:
        raise ValueError('No measured poses saved. Save the current pose first.') from None
    if data.get('schema_version') != 1 or data.get('twin') != twin:
        raise ValueError('Saved poses belong to another twin or unsupported format.')
    item = data.get('poses', {}).get(name)
    if not isinstance(item, dict) or item.get('source') != 'edge_follower':
        raise ValueError(f'No measured pose named {name!r} for this twin.')
    degrees = item.get('degrees')
    if not isinstance(degrees, dict) or set(degrees) != set(robot.JOINTS) or not all(robot.finite(v) for v in degrees.values()):
        raise ValueError('Saved pose must contain six finite joint angles in degrees.')
    return dict(degrees)


def save_pose(channel, name, timeout=8):
    if not re.fullmatch(r'[a-z][a-z0-9_-]{0,39}', name):
        raise ValueError('Invalid pose name.')
    data = json.loads(path().read_text()) if path().exists() else {'schema_version': 1, 'twin': channel.twin, 'poses': {}}
    if data.get('schema_version') != 1 or data.get('twin') != channel.twin:
        raise ValueError('Existing pose file belongs to another twin or unsupported format.')
    if name in data['poses']:
        raise ValueError('Pose already exists; it was not overwritten.')
    state = channel.wait_state(timeout)
    item = {'source': 'edge_follower', 'timestamp': state['timestamp'],
            'session_id': state['session_id'], 'saved_at': time.time(),
            'degrees': {n: math.degrees(state['positions'][j]) for n, j in robot.JOINTS.items()}}
    data['poses'][name] = item
    target = path()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix('.tmp')
    with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 'w') as f:
        json.dump(data, f, indent=2, allow_nan=False)
        f.write('\n')
    temporary.replace(target)
    return {'status': 'pose_saved', 'pose': name, 'motion_sent': False, **item}
