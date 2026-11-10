"""Versioned wire contract; pure math, no connection or hardware access."""
import math

PROTOCOL = 'so101.workflow.v1'
HZ = 30
MAX_CHUNKS = 256
COORDINATED = 'coordinated_joints_v1'
JOINTS = dict(zip(('shoulder_pan', 'shoulder_lift', 'elbow_flex',
                   'wrist_flex', 'wrist_roll', 'gripper'),
                  ('_1', '_2', '_3', '_4', '_5', '_6')))


def validate_specs(specs):
    """Driver-derived calibrated coordinates, never guessed client limits."""
    if not isinstance(specs, dict) or set(specs) != set(JOINTS.values()):
        raise ValueError('Six calibrated joint specifications required.')
    for key, spec in specs.items():
        unit = 'percent' if key == '_6' else 'radians'
        if not isinstance(spec, dict) or spec.get('unit') != unit:
            raise ValueError('Incorrect coordinated joint units.')
        limits = spec.get('limits')
        maximum = 45 if unit == 'percent' else math.radians(45)
        tolerance = 2 if unit == 'percent' else math.radians(1.5)
        if (not isinstance(limits, list) or len(limits) != 2
                or not all(finite(x) for x in limits) or limits[0] >= limits[1]
                or not finite(spec.get('peak_speed')) or not 0 < spec['peak_speed'] <= maximum
                or not finite(spec.get('tolerance')) or not 0 < spec['tolerance'] <= tolerance
                or (unit == 'percent' and limits != [0, 100])):
            raise ValueError('Invalid coordinated joint bounds, speed or tolerance.')
    return specs


def coordinated_command(commands):
    """One simultaneous group; gripper opening is a calibrated percentage."""
    joints = {}
    for item in commands:
        joint = item.get('joint')
        if joint not in JOINTS or JOINTS[joint] in joints:
            raise ValueError('Unknown or duplicate joint in simultaneous group.')
        kind = item.get('type')
        if joint == 'gripper':
            field = {'absolute': 'target_percent', 'relative': 'delta_percent'}.get(kind)
            value, unit = item.get(field), 'percent'
        else:
            field = {'absolute': 'target_degrees', 'relative': 'delta_degrees'}.get(kind)
            value, unit = item.get(field), 'radians'
        if field is None or not finite(value):
            raise ValueError('Finite joint target with explicit units required.')
        joints[JOINTS[joint]] = dict(type=kind, value=value if unit=='percent' else math.radians(value), unit=unit)
    command = dict(type='coordinated', joints=joints)
    validate_coordinated_command(command)
    return command


def validate_coordinated_command(command):
    if (not isinstance(command, dict) or set(command) != {'type', 'joints'}
            or command['type'] != 'coordinated' or not isinstance(command['joints'], dict)
            or not 1 <= len(command['joints']) <= 6):
        raise ValueError('Expected a coordinated joint group.')
    for key, item in command['joints'].items():
        if (key not in JOINTS.values() or not isinstance(item, dict)
                or set(item) != {'type', 'value', 'unit'} or item['type'] not in {'absolute','relative'}
                or item['unit'] != ('percent' if key=='_6' else 'radians') or not finite(item['value'])):
            raise ValueError('Invalid coordinated joint command or units.')


def coordinated_target(start, command, specs):
    """Validate without generating samples; omitted joints hold measured start."""
    validate_specs(specs)
    validate_coordinated_command(command)
    if not isinstance(start, dict) or set(start) != set(specs):
        raise ValueError('Fresh complete six-joint start required.')
    target = dict(start)
    for key, item in command['joints'].items():
        target[key] = item['value'] + (start[key] if item['type']=='relative' else 0)
    duration = .5
    for key, spec in specs.items():
        low, high = spec['limits']
        if (not finite(start[key]) or not finite(target[key]) or not finite(target[key]-start[key])
                or not low <= start[key] <= high or not low <= target[key] <= high):
            raise ValueError('Joint start or target outside calibrated/taught bounds: '+key)
        duration = max(duration, 1.5*abs(target[key]-start[key])/spec['peak_speed'])
    steps = math.ceil(duration*HZ)
    if steps+1 > MAX_CHUNKS:
        raise ValueError('Trajectory exceeds driver chunk limit.')
    return target, steps


def coordinated_trajectory(start, command, specs):
    target, steps = coordinated_target(start, command, specs)
    values = [{key: start[key]+(target[key]-start[key])*(i/steps)**2*(3-2*i/steps)
               for key in start} for i in range(steps+1)]
    values[-1] = dict(target)
    return values, steps/HZ


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def topics(prefix, twin):
    root = f'{prefix}cyberwave/twin/{twin}/workflow/v1'
    return root + '/request', root + '/response'


def validate_segment(start, command, limits, speed=45):
    """Validate coordinates and timing without building interpolation samples."""
    if (not finite(start) or not isinstance(limits, list) or len(limits) != 2
            or not all(finite(x) for x in limits) or limits[0] >= limits[1]
            or not finite(speed) or not 0 < speed <= 90):
        raise ValueError('Invalid trajectory start, bounds or speed.')
    if not isinstance(command, dict):
        raise ValueError('Trajectory command must be an object.')
    kind = command.get('type')
    field = {'absolute': 'target_radians', 'relative': 'delta_radians'}.get(kind)
    if field is None or set(command) != {'type', 'joint', field} or command['joint'] != '_1' or not finite(command[field]):
        raise ValueError('Expected one finite absolute or relative base command in radians.')
    target = command[field] if kind == 'absolute' else start + command[field]
    if (not finite(target) or not finite(target-start) or not limits[0] <= start <= limits[1]
            or not limits[0] <= target <= limits[1]):
        raise ValueError('Base start or target outside taught bounds.')
    steps = math.ceil(max(1.5 * abs(target-start) / math.radians(speed), .5) * HZ)
    if steps + 1 > MAX_CHUNKS:
        raise ValueError('Trajectory exceeds driver chunk limit.')
    return target, steps


def trajectory(start, command, limits, speed=45):
    """Radians in/out; interpolation is generated only by the driver."""
    target, steps = validate_segment(start, command, limits, speed)
    values = [start + (target-start) * (i/steps)**2 * (3-2*i/steps) for i in range(steps+1)]
    values[-1] = target
    return values, steps/HZ


def wire_command(item):
    kind = item.get('type')
    field = {'absolute': 'target_degrees', 'relative': 'delta_degrees'}.get(kind)
    if item.get('joint') != 'shoulder_pan' or field is None or not finite(item.get(field)):
        raise ValueError('Only finite base commands support local smooth trajectories.')
    return {'type': kind, 'joint': '_1', field.replace('degrees', 'radians'): math.radians(item[field])}
