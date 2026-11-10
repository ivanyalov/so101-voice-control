"""Synthetic six-motor register bus for offline workflow transition tests."""
from types import SimpleNamespace


def add_torque_bus(follower):
    names = list(follower.motors)
    follower.calibration = {n: SimpleNamespace(id=follower.motors[n].id,
        homing_offset=100, range_min=10, range_max=4000) for n in names}
    regs = {register: {n: value for n in names} for register, value in (
        ('Homing_Offset', 100), ('Min_Position_Limit', 10), ('Max_Position_Limit', 4000),
        ('Operating_Mode', 0), ('Torque_Enable', int(follower.torque_enabled)),
        ('Present_Position', 1700), ('Goal_Position', 3200))}
    bus = SimpleNamespace(events=[], regs=regs, fail_read=None)
    def read(register, **kwargs):
        if register == bus.fail_read:
            return {}
        bus.events.append(('read', register))
        return dict(regs[register])
    def write(register, values, **kwargs):
        bus.events.append(('write', register, dict(values)))
        regs[register] = dict(values)
    def enable():
        bus.events.append('enable')
        regs['Torque_Enable'] = {n: 1 for n in names}
        follower.torque_enabled = True
    def disable():
        bus.events.append('disable')
        regs['Torque_Enable'] = {n: 0 for n in names}
        follower.torque_enabled = False
    bus.sync_read, bus.sync_write = read, write
    follower.bus = bus
    follower.enable_torque, follower.disable_torque = enable, disable
    return bus
