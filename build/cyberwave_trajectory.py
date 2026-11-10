"""Optional locally timed 30 Hz chunks for the existing SO101 writer queue."""
import math
import time


def schedule_chunks(data, count):
    if data.get('local_trajectory_hz') is None:
        return [{} for _ in range(count)]
    if data['local_trajectory_hz'] != 30 or not 1 <= count <= 256:
        raise ValueError('Local trajectories require 30 Hz and 1..256 chunks')
    clock = {'start': None}
    return [{'local_clock': clock, 'local_offset': i / 30} for i in range(count)]


def wait_for_chunk(meta, stop_event):
    if not meta or 'local_clock' not in meta:
        return True
    clock = meta['local_clock']
    if clock['start'] is None:
        clock['start'] = time.monotonic()
    deadline = clock['start'] + meta['local_offset']
    if time.monotonic() - deadline > 0.15:
        raise RuntimeError('Local trajectory writer stalled')
    return not stop_event.wait(max(0, deadline - time.monotonic()))
